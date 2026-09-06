"""The public source tree must not depend on private machine files."""

import json
import unittest
from pathlib import Path

from delta_data import MAP_IMAGES

ROOT = Path(__file__).resolve().parent


class SourceDistributionTests(unittest.TestCase):
    def test_all_map_paths_have_distributable_assets(self):
        for asset in MAP_IMAGES.values():
            with self.subTest(asset=asset):
                self.assertTrue((ROOT / "web" / asset.lstrip("/")).is_file())
        self.assertTrue((ROOT / "tools/generate_map_placeholders.py").is_file())

    def test_public_onboarding_files_exist(self):
        for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "SECURITY.md",
                     "THIRD_PARTY_NOTICES.md", "licenses/Feather-MIT.txt",
                     "licenses/Inno-Setup.txt", "package-lock.json"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_no_local_operation_paths_in_public_docs(self):
        documents = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]
        for document in documents:
            text = document.read_text(encoding="utf-8")
            self.assertNotIn("ubuntu.pem", text, document.name)
            self.assertNotIn("C:\\Users\\", text, document.name)
            self.assertNotIn("C:/Users/", text, document.name)
            self.assertNotIn("HANDOFF.md", text, document.name)

    def test_npm_lock_has_only_public_registry_dependencies(self):
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        for package in lock["packages"].values():
            resolved = package.get("resolved", "")
            if resolved:
                self.assertTrue(resolved.startswith("https://registry.npmjs.org/"), resolved)
                self.assertIn("integrity", package)


if __name__ == "__main__":
    unittest.main()
