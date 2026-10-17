from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VersionDocsTests(unittest.TestCase):
    def test_readme_keeps_latest_and_feature_versions_separate(self):
        manifest = (ROOT / "pixi.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "(\d+\.\d+\.\d+)"$', manifest, re.MULTILINE)
        self.assertIsNotNone(match)
        version = match.group(1)
        feature = ".".join(version.split(".")[:2]) + ".0"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        history = (ROOT / "docs/changelog.md").read_text(encoding="utf-8")
        self.assertIn(f"最新版本：**v{version}**", readme)
        self.assertIn(f"当前功能版本：**v{feature}**", readme)
        self.assertIn(f"**v{feature} 主要功能**", readme)
        self.assertIn(f"## v{version} · ", history)
        self.assertIn(f"## v{feature} · ", history)

    def test_pr_workflow_is_documented_for_agents_and_contributors(self):
        for path in ("AGENTS.md", "CLAUDE.md", "docs/development.md", ".github/PULL_REQUEST_TEMPLATE.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("main", text, path)
            self.assertIn("PR", text, path)


if __name__ == "__main__":
    unittest.main()
