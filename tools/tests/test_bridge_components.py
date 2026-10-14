"""Exercise the extracted components independently of a Feishu bridge."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from tools.claude_feishu import presentation
from tools.claude_feishu.sessions import SessionStore


ROOT = Path(__file__).resolve().parents[2]


def test_stores_are_isolated_and_reset_keeps_usage(tmp_path):
    first = SessionStore(tmp_path / "first.json")
    second = SessionStore(tmp_path / "second.json")
    sid, _ = first.get_or_create("chat")
    first.entry("chat")["workdir"] = "/workspace"
    first.capture("chat", "dsh", "dsh-id", "/workspace")
    state = presentation.RunState("dsh")
    state.turns = 2
    state.total_usage = {"output_tokens": 40}
    first.update_stats("chat", state, 3)
    first.reset("chat", ("dsh",))
    first.capture("chat", "dsh", "stale-id", "/workspace", epoch=0)
    assert second.chats == {}
    restored = SessionStore(first.path)
    assert restored.bucket("chat", "claude")["session_id"] == sid
    assert restored.workdir("chat") == "/workspace"
    assert "session_id" not in restored.bucket("chat", "dsh")
    assert restored.bucket("chat", "dsh")["total_usage"]["output_tokens"] == 40
    assert first.path.stat().st_mode & 0o777 == 0o600


def test_display_settings_belong_to_each_run(monkeypatch):
    monkeypatch.delenv("CLAUDE_FEISHU_CLAUDE_MODEL_NAME", raising=False)
    first = presentation.RunState(
        context_window=1000, default_workdir="/first", claude_model="alpha"
    )
    second = presentation.RunState(
        context_window=2000, default_workdir="/second", claude_model="beta"
    )
    event = {"type": "assistant", "message": {"usage": {"input_tokens": 100}}}
    for state in (first, second):
        state.observe(event)
    first_card = presentation.card(first, 1, done="first")
    second_card = presentation.card(second, 1, done="second")
    assert first_card["header"]["subtitle"]["content"] == "alpha · /first · ctx 10%"
    assert second_card["header"]["subtitle"]["content"] == "beta · /second · ctx 5%"
    assert second_card["elements"][0]["text"]["content"] == "second"


def test_state_factory_failure_does_not_start_a_child(tmp_path):
    from tools.claude_feishu.runner import AgentRunner

    runner = AgentRunner(SessionStore(tmp_path / "sessions.json"))

    def broken_state(_):
        raise RuntimeError("state initialization failed")

    with patch("tools.claude_feishu.runner.subprocess.Popen") as spawn:
        with pytest.raises(RuntimeError, match="state initialization failed"):
            runner.run(
                "task", "chat", claude_cli=sys.executable, settings="",
                default_workdir=str(tmp_path), session_context_enabled=True,
                timeout=1, progress_interval=0, state_factory=broken_state,
            )
    spawn.assert_not_called()
    assert runner.active_process is None


def test_runner_and_rendering_work_without_feishu_sdk(tmp_path):
    script = r"""
import builtins
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def import_without_feishu(name, *args, **kwargs):
    if name.startswith("lark_oapi") or name == "tools.claude_feishu.bridge":
        raise AssertionError("components must not import the Feishu bridge")
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_feishu

from tools.claude_feishu.presentation import RunState, card
from tools.claude_feishu.runner import AgentRunner
from tools.claude_feishu.sessions import SessionStore

directory = Path(sys.argv[2])
events = [
    {"type": "assistant", "message": {"usage": {"input_tokens": 50}}},
    {"type": "result", "result": "completed", "usage": {"output_tokens": 12}},
]
cli = directory / "fake-agent"
cli.write_text("#!" + sys.executable + "\nimport json\nfor event in " + repr(events) + ":\n    print(json.dumps(event), flush=True)\n")
cli.chmod(0o700)
sessions = SessionStore(directory / "sessions.json")
runner = AgentRunner(sessions)
other = AgentRunner(SessionStore(directory / "other.json"))
answer, state = runner.run(
    "sample task", "chat", claude_cli=str(cli), settings="",
    default_workdir=str(directory), session_context_enabled=True,
    timeout=5, progress_interval=0, state_factory=RunState,
)
assert answer == "completed" and state.returncode == 0
assert state.context_used() == 50
assert sessions.bucket("chat", "claude")["session_id"]
assert card(state, 1, done=answer)["header"]["template"] == "green"
assert runner.active_process is None
runner.shutdown()
assert runner.stopping.is_set() and not other.stopping.is_set()
assert other.sessions.chats == {}
print(json.dumps({"answer": answer, "context": state.context_used()}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"answer": "completed", "context": 50}
