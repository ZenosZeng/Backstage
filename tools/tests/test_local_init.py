"""Local onboarding must not need S3 or overwrite user-owned entry points."""
import json
from pathlib import Path
import shutil
from unittest import mock

import pytest

from scripts import doctor, sync


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "workspace/.agents"
    root.mkdir(parents=True)
    public = Path(sync.__file__).parents[1]
    for name in ("config.template.json", "pixi.toml"):
        shutil.copyfile(public / name, root / name)
    shutil.copytree(public / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(public / "examples", root / "examples")
    return root, tmp_path / "agent-home"


def init(root, home, *, dry_run=False, machine="local-test"):
    args = sync.parser().parse_args(["--root", str(root), *(["--dry-run"] if dry_run else []),
                                     "init", "--local", "--machine-id", machine])
    with mock.patch.object(Path, "home", return_value=home), \
            mock.patch.object(sync, "run_mc", side_effect=AssertionError("no S3")):
        return sync.command_init(root, args)


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_local_init_dry_run_installs_links_and_is_idempotent(setup):
    root, home = setup
    before = files(root.parent)
    init(root, home, dry_run=True)
    assert files(root.parent) == before and not home.exists()
    init(root, home)
    config = json.loads((root / "config.json").read_text())
    assert config["sync"]["enabled"] is False and not config["sync"]["remote"]
    assert config["machine_id"] == "local-test"
    assert (root.parent / "AGENTS.md").resolve() == root / ".share/agent/prompts/AGENTS.md"
    assert (home / ".claude/skills/backstage-memory/SKILL.md").is_file()
    assert all((root / ".share" / name).is_dir() for name in sync.SHARED_DIRS)
    with mock.patch.object(sync, "mirror") as mirror:
        sync.upload_shared(root, config | {"sync": config["sync"] | {"remote": "test/bucket/prefix"}}, dry_run=False)
    assert mirror.call_count == len(sync.SHARED_DIRS)
    custom = root / ".share/knowledge/_workspace.md"
    custom.write_text("my knowledge")
    after = files(root.parent)
    init(root, home)
    assert files(root.parent) == after
    report = doctor.inspect_workspace(root, home=home)
    assert next(c["state"] for c in report["checks"] if c["name"]=="Skill 与入口链接") == "ok"
    assert report["skills"][0]["minimum_version"] == "0.8.0"
    assert next(c["state"] for c in report["checks"] if c["name"]=="入口一致性") == "ok"


@pytest.mark.parametrize("conflict", ["AGENTS.md", "CLAUDE.md", "skill"])
def test_conflicting_entry_is_rejected_before_any_install(setup, conflict):
    root, home = setup
    path = home / ".codex/skills/backstage-memory" if conflict=="skill" else root.parent / conflict
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("user instructions")
    before = files(root.parent)
    with pytest.raises(sync.SyncError, match="不会覆盖"):
        init(root, home)
    assert files(root.parent) == before
    assert path.read_text() == "user instructions"
    assert not (root / "config.json").exists()


@pytest.mark.parametrize("parent", [".codex", ".claude/skills"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_entry_parent_conflict_is_rejected_before_install(setup, parent, dry_run):
    root, home = setup
    path = home / parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("user-owned file")
    before = files(root.parent)
    with pytest.raises(sync.SyncError, match="父路径"):
        init(root, home, dry_run=dry_run)
    assert files(root.parent) == before
    assert not (root / "config.json").exists()
    assert not (root / ".share").exists()
    assert path.read_text() == "user-owned file"


def test_existing_s3_configuration_and_identity_are_preserved(setup):
    root, home = setup
    init(root, home)
    with pytest.raises(sync.SyncError, match="machine_id"):
        init(root, home, machine="other")
    path = root / "config.json"
    config = json.loads(path.read_text())
    config["sync"]["enabled"] = True
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    with pytest.raises(sync.SyncError, match="启用了 S3"):
        init(root, home)
    assert path.read_bytes() == before


def test_skill_declared_minimum_version_flags_old_core_and_unknown(setup):
    root, home = setup
    init(root, home)
    (root / "pixi.toml").write_text('[workspace]\nversion = "0.7.1"\n')
    assert doctor.inspect_workspace(root, home=home)["skills"][0]["state"] == "outdated"
    skill = root / ".share/agent/skills/common/backstage-memory/SKILL.md"
    skill.write_text("---\nname: custom\n---\nold Skill without version declaration")
    assert doctor.inspect_workspace(root, home=home)["skills"][0]["state"] == "unknown"


def test_workspace_path_must_match_the_bundled_prompt_location(setup):
    root, home = setup
    args = sync.parser().parse_args(["init", "--local", "--machine-id", "local-test",
                                     "--workspace-root", str(root.parent / "other")])
    with mock.patch.object(Path, "home", return_value=home), pytest.raises(sync.SyncError, match=".agents"):
        sync.command_init(root, args)
    assert not (root / "config.json").exists() and not home.exists()
