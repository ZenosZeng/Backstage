"""Run the public starter against an empty workspace and a fake object store."""

from __future__ import annotations

from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "onboarding_sync", ROOT / "scripts/sync.py"
)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.guide = (ROOT / "examples/README.md").read_text()

    def tearDown(self):
        self.temporary.cleanup()

    def seed_core(self, workspace):
        root = workspace / ".agents"
        root.mkdir(parents=True)
        shutil.copyfile(ROOT / "config.template.json", root / "config.template.json")
        shutil.copytree(
            ROOT / "scripts",
            root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        return root

    def call(self, root, *args):
        result = subprocess.run(
            [sys.executable, *args],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def seed_local(self, workspace):
        root = self.seed_core(workspace)
        shutil.copyfile(root / "config.template.json", root / "config.json")
        shutil.copytree(ROOT / "examples/workspace", root / ".share")
        for name in ("docs", "setup"):
            (root / ".share" / name).mkdir()
        # Execute the actual configuration snippet shown in the guide.
        snippets = re.findall(r"python3 - <<'PY'\n(.*?)\nPY", self.guide, re.S)
        self.call(root, "-c", snippets[0])
        return root

    def local_workflow(self, root):
        blocks = re.findall(r"```bash\n(.*?)\n```", self.guide, re.S)
        commands = next(
            b for b in blocks if b.startswith("python3 scripts/memory.py validate")
        )
        # Keep the documented commands, using the current interpreter on CI too.
        commands = commands.replace("python3 ", f"{sys.executable} ")
        result = subprocess.run(
            ["bash", "-e", "-c", commands],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def test_starter_can_write_query_and_close_without_s3(self):
        root = self.seed_local(self.directory / "local")
        self.local_workflow(root)
        config = json.loads((root / "config.json").read_text())
        self.assertFalse(config["sync"]["enabled"])
        self.assertFalse(config["sync"]["initialized"])
        self.assertEqual(config["sync"]["remote"], "")
        brief = json.loads(
            self.call(
                root,
                "scripts/memory.py",
                "brief",
                "--task",
                "demo-onboarding",
                "--json",
            )
        )
        self.assertEqual(brief["tasks"][0]["state"], "closed")
        records = brief["sections"]["事实与验证记录"]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["sources"])
        self.assertFalse((root.parent / "AGENTS.md").exists())

    def test_starter_can_publish_and_join_an_empty_shared_workspace(self):
        first_workspace = self.directory / "first"
        first = self.seed_local(first_workspace)
        self.local_workflow(first)
        snippets = re.findall(r"python3 - <<'PY'\n(.*?)\nPY", self.guide, re.S)
        self.call(first, "-c", snippets[1])
        remote_prefix = "my-store/my-bucket/backstage"
        storage = self.directory / "objects"

        def storage_path(value):
            if isinstance(value, str) and value.startswith(remote_prefix):
                return storage / value[len(remote_prefix) :].lstrip("/")
            return Path(value)

        def has_objects(remote, **kwargs):
            return any(p.is_file() for p in storage_path(remote).rglob("*"))

        def mirror(source, destination, **kwargs):
            source, destination = storage_path(source), storage_path(destination)
            if kwargs.get("remove") and destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, dirs_exist_ok=True)

        first_home = self.directory / "first-home"
        first_home.mkdir()
        with (
            mock.patch.object(SYNC, "remote_has_objects", side_effect=has_objects),
            mock.patch.object(SYNC, "mirror", side_effect=mirror),
            mock.patch.object(
                SYNC,
                "run_mc",
                side_effect=AssertionError("real S3 access is forbidden in this test"),
            ),
            mock.patch.object(SYNC.Path, "home", return_value=first_home),
        ):
            SYNC.command_init(
                first,
                Namespace(
                    push=True, allow_non_writer=False, dry_run=False, force=False
                ),
            )
            SYNC.command_sync(first, Namespace(dry_run=False))
            self.assertTrue(
                json.loads((first / "config.json").read_text())["sync"]["initialized"]
            )
            self.assert_agent_links(first_workspace, first_home)

            second_workspace = self.directory / "second"
            second = self.seed_core(second_workspace)
            second_home = self.directory / "second-home"
            second_home.mkdir()
            with mock.patch.object(SYNC.Path, "home", return_value=second_home):
                SYNC.command_init(
                    second,
                    Namespace(
                        push=False,
                        remote=remote_prefix,
                        machine_id="dev-machine-02",
                        machine_role="development",
                        workspace_root=str(second_workspace),
                        shared_writer=False,
                        clear_proxy=True,
                        dry_run=False,
                        reuse_machine=False,
                        force=False,
                    ),
                )
            config = json.loads((second / "config.json").read_text())
            self.assertFalse(config["shared_writer"])
            self.assertEqual(config["machine_id"], "dev-machine-02")
            self.assert_agent_links(second_workspace, second_home)
            brief = json.loads(
                self.call(
                    second,
                    "scripts/memory.py",
                    "brief",
                    "--task",
                    "demo-onboarding",
                    "--json",
                )
            )
            self.assertEqual(brief["tasks"][0]["state"], "closed")
            self.assertTrue(brief["sections"]["事实与验证记录"])

    def assert_agent_links(self, workspace, agent_home):
        shared = workspace / ".agents/.share"
        for name in ("AGENTS.md", "CLAUDE.md"):
            self.assertEqual(
                (workspace / name).resolve(), shared / "agent/prompts" / name
            )
        for name in (".codex", ".claude", ".kimi-code"):
            self.assertEqual(
                (agent_home / name / "skills/backstage-memory").resolve(),
                shared / "agent/skills/common/backstage-memory",
            )
