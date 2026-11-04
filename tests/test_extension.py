from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.extension_dir = self.repo_root / "extension"
        self.manifest_path = self.extension_dir / "manifest.json"
        self.background_js = self.extension_dir / "src" / "background.js"
        self.content_js = self.extension_dir / "src" / "content.js"

    def test_manifest_exists_and_is_valid_json(self) -> None:
        self.assertTrue(self.manifest_path.is_file(), "manifest.json must exist")
        with self.manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("manifest_version"), 3)

    def test_manifest_supports_both_chrome_and_firefox(self) -> None:
        with self.manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # Background definition for dual Chrome/Firefox MV3 compatibility
        background = data.get("background", {})
        self.assertIn("service_worker", background, "Chrome MV3 requires service_worker")
        self.assertEqual(background.get("service_worker"), "src/background.js")

        self.assertIn("scripts", background, "Firefox MV3 requires scripts fallback")
        self.assertEqual(background.get("scripts"), ["src/background.js"])
        self.assertEqual(background.get("type"), "module")

        # Gecko extension ID required for Firefox MV3
        gecko_settings = data.get("browser_specific_settings", {}).get("gecko", {})
        self.assertTrue(gecko_settings.get("id"), "Firefox MV3 requires gecko.id")

        # Required permissions
        permissions = set(data.get("permissions", []))
        for required in ("alarms", "tabs", "scripting"):
            self.assertIn(required, permissions, f"Missing required permission: {required}")

        # Host permissions for broker loopback and target AI services
        host_permissions = set(data.get("host_permissions", []))
        self.assertIn("http://127.0.0.1/*", host_permissions)
        self.assertIn("http://localhost/*", host_permissions)

    def test_extension_scripts_exist_and_avoid_raw_chrome_globals(self) -> None:
        self.assertTrue(self.background_js.is_file(), "src/background.js must exist")
        self.assertTrue(self.content_js.is_file(), "src/content.js must exist")

        bg_text = self.background_js.read_text(encoding="utf-8")
        content_text = self.content_js.read_text(encoding="utf-8")

        # Ensure browserAPI fallback is established in both scripts
        self.assertIn("globalThis.browser || globalThis.chrome", bg_text)
        self.assertIn("globalThis.browser || globalThis.chrome", content_text)

        # Check that chrome.<api> is not used directly without browserAPI
        raw_chrome_pattern = re.compile(r"\bchrome\.(runtime|tabs|alarms|scripting)\b")
        bg_matches = raw_chrome_pattern.findall(bg_text)
        self.assertEqual(bg_matches, [], f"Raw chrome.* calls found in background.js: {bg_matches}")

        content_matches = raw_chrome_pattern.findall(content_text)
        self.assertEqual(content_matches, [], f"Raw chrome.* calls found in content.js: {content_matches}")

    def test_javascript_syntax_with_node_if_available(self) -> None:
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node is not installed; skipping JS syntax validation")

        for js_file in (self.background_js, self.content_js):
            result = subprocess.run(
                [node_bin, "--check", str(js_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"Syntax error in {js_file.name}:\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
