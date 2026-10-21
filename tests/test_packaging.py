#!/usr/bin/env python3
"""Release hygiene: every manifest and version string agrees, and every path a
manifest or hook config points at exists.  Run: python -m unittest discover tests"""

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def load_json(rel):
    return json.loads(read(rel))


class TestVersionsAgree(unittest.TestCase):
    def versions(self):
        found = {
            "plugin.json": load_json("plugin.json")["version"],
            ".claude-plugin/plugin.json": load_json(".claude-plugin/plugin.json")["version"],
            "pyproject.toml": tomllib.loads(read("pyproject.toml"))["project"]["version"],
            "SERVER_VERSION": re.search(r'^SERVER_VERSION = "([^"]+)"', read("scripts/mitosis-mcp-server.py"), re.M).group(1),
            "SKILL.md": re.search(r"— v(\d+\.\d+\.\d+)\)", read("skills/orchestrate/SKILL.md")).group(1),
            "rules/AGENTS.md": re.search(r"\(v(\d+\.\d+\.\d+)\)", read("rules/AGENTS.md").splitlines()[0]).group(1),
            "CHANGELOG.md": re.search(r"^## (\d+\.\d+\.\d+)", read("CHANGELOG.md"), re.M).group(1),
        }
        for entry in load_json(".claude-plugin/marketplace.json")["plugins"]:
            if entry["name"] == "mitosis-orchestrator":
                found["marketplace.json"] = entry["version"]
        codex_manifest = ROOT / ".codex-plugin" / "plugin.json"
        if codex_manifest.exists():
            found[".codex-plugin/plugin.json"] = json.loads(codex_manifest.read_text(encoding="utf-8"))["version"]
        return found

    def test_single_version(self):
        found = self.versions()
        self.assertEqual(len(set(found.values())), 1, found)

    def test_readme_examples_name_current_version(self):
        version = self.versions()["plugin.json"]
        for rel in ("README.md", "README.zh-TW.md"):
            with self.subTest(readme=rel):
                self.assertIn(f"(enabled, {version})", read(rel))


class TestManifestPaths(unittest.TestCase):
    def test_claude_agents_exist_with_dot_slash(self):
        # Claude Code loads plugin agents only from "./"-prefixed paths.
        for rel in load_json(".claude-plugin/plugin.json")["agents"]:
            with self.subTest(agent=rel):
                self.assertTrue(rel.startswith("./"), rel)
                self.assertTrue((ROOT / rel).is_file(), rel)

    def test_hook_commands_point_at_existing_scripts(self):
        configs = ["hooks.json", "hooks/hooks.json"]
        if (ROOT / "hooks" / "codex-hooks.json").exists():
            configs.append("hooks/codex-hooks.json")
        script_re = re.compile(r"(?:\}|\.)/(scripts/[\w.-]+\.py)")
        for rel in configs:
            text = read(rel)
            json.loads(text)
            scripts = script_re.findall(text)
            with self.subTest(config=rel):
                self.assertTrue(scripts, f"{rel} runs no plugin script")
                for script in scripts:
                    self.assertTrue((ROOT / script).is_file(), f"{rel}: {script}")

    def test_hook_modes_are_known(self):
        handlers = re.search(r"^HANDLERS = \{(.*?)^\}", read("scripts/enforce-orchestrator.py"), re.M | re.S).group(1)
        known = set(re.findall(r'"([\w-]+)":', handlers))
        mode_re = re.compile(r'enforce-orchestrator\.py\\?"? ([\w-]+)')
        for rel in ("hooks.json", "hooks/hooks.json", "hooks/codex-hooks.json"):
            if not (ROOT / rel).exists():
                continue
            for mode in mode_re.findall(read(rel)):
                with self.subTest(config=rel, mode=mode):
                    self.assertIn(mode, known)


if __name__ == "__main__":
    unittest.main()
