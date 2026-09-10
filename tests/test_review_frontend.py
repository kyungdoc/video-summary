"""Static review checks plus optional dependency-free Node DOM regression tests.

Node is a development-only test tool, never a video-summary runtime requirement.
Use VIDEO_SUMMARY_TEST_NODE=/path/to/node if it is not available on PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path


class _MarkupInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.labels: list[str] = []
        self.scripts: list[str | None] = []
        self.stylesheets: list[str | None] = []
        self.inline_handlers: list[str] = []
        self.inline_styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(str(attributes["id"]))
        if tag == "label" and attributes.get("for"):
            self.labels.append(str(attributes["for"]))
        if tag == "script":
            self.scripts.append(attributes.get("src"))
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href"))
        self.inline_handlers.extend(key for key, _ in attrs if key.lower().startswith("on"))
        if "style" in attributes:
            self.inline_styles.append(tag)


class ReviewFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = resources.files("video_summary").joinpath("review_static")
        cls.html = cls.assets.joinpath("index.html").read_text(encoding="utf-8")
        cls.javascript = cls.assets.joinpath("app.js").read_text(encoding="utf-8")
        cls.css = cls.assets.joinpath("style.css").read_text(encoding="utf-8")
        cls.inventory = _MarkupInventory()
        cls.inventory.feed(cls.html)

    def test_static_files_are_accessible_as_package_resources(self) -> None:
        for name in ("index.html", "app.js", "style.css"):
            with self.subTest(name=name):
                self.assertTrue(self.assets.joinpath(name).is_file())
                self.assertGreater(len(self.assets.joinpath(name).read_bytes()), 0)

    def test_javascript_ids_and_labels_have_unique_html_targets(self) -> None:
        self.assertEqual(len(self.inventory.ids), len(set(self.inventory.ids)))
        ids = set(self.inventory.ids)
        for identifier in re.findall(r'\$\("([^"\n]+)"\)', self.javascript):
            self.assertIn(identifier, ids)
        for identifier in self.inventory.labels:
            self.assertIn(identifier, ids)

    def test_scripts_and_styles_only_use_packaged_same_origin_urls(self) -> None:
        self.assertEqual(self.inventory.scripts, ["/static/app.js"])
        self.assertEqual(self.inventory.stylesheets, ["/static/style.css"])
        self.assertFalse(self.inventory.inline_handlers)
        self.assertFalse(self.inventory.inline_styles)
        self.assertNotRegex(self.css, r"@import\b|url\(\s*['\"]?https?:")
        self.assertNotRegex(self.javascript, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(")

    def test_optional_node_regressions(self) -> None:
        configured = os.environ.get("VIDEO_SUMMARY_TEST_NODE")
        node = shutil.which(configured) if configured else shutil.which("node")
        if configured and not node:
            self.fail("VIDEO_SUMMARY_TEST_NODE must name an executable Node.js binary")
        if not node:
            self.skipTest("Node.js is optional; set VIDEO_SUMMARY_TEST_NODE to run DOM regression tests")
        harness = Path(__file__).with_name("review_frontend.test.cjs")
        result = subprocess.run(
            [node, "--test", str(harness)],
            cwd=harness.parent.parent,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
