"""Decision commands use the existing human whitelist; no live Feishu or CLI."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import Mock

import pytest

from tools.watchdog.interaction import Queue


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_FEISHU_STATE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("CLAUDE_FEISHU_SESSIONS", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("FEISHU_ALLOW_OPEN_IDS", "allowed")
    monkeypatch.setenv("AGENTS_LOCAL_ROOT", str(tmp_path / ".local"))
    path = Path(__file__).resolve().parents[1] / "claude_feishu/bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_interaction_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_send_reply", Mock(return_value=True))
    monkeypatch.setattr(module, "_execute_and_reply", Mock())
    return module


@pytest.fixture
def send(bridge):
    sequence = 0
    def receive(text, *, sender="allowed", sender_type="user", group=False, mention=True):
        nonlocal sequence
        sequence += 1
        created = str(int(time.time() * 1000) + sequence)
        message = SimpleNamespace(
            message_id=f"m{sequence}", create_time=created,
            chat_id="chat", chat_type="group" if group else "p2p", message_type="text",
            content=json.dumps({"text": text}),
            mentions=[SimpleNamespace(id=SimpleNamespace(open_id=bridge.BOT_OPEN_ID))] if group and mention else [],
        )
        event = SimpleNamespace(
            header=SimpleNamespace(event_id=f"e{sequence}", create_time=created),
            event=SimpleNamespace(message=message, sender=SimpleNamespace(
                sender_type=sender_type, sender_id=SimpleNamespace(open_id=sender))),
        )
        bridge.handle_message(event)
        return event
    return receive


def test_questions_list_pending_only_without_creating_an_empty_queue(bridge, send):
    queue = Queue()
    send("/questions")
    assert "没有待决策" in bridge._send_reply.call_args.args[1]
    assert not queue.path.exists()
    first = queue.ask("选择哪个方案？", agent="codex", context="当前阻塞原因", task="demo-task", options=["方案 A", "方案 B"])
    answered = queue.ask("之前的问题", agent="agent")
    queue.answer(answered["id"], "完成")
    send("/questions")
    message = bridge._send_reply.call_args.args[1]
    assert first["id"] in message and "当前阻塞原因" in message and "demo-task" in message
    assert "方案 A；方案 B" in message and answered["id"] not in message
    bridge._execute_and_reply.assert_not_called()


def test_answer_records_literal_text_while_an_agent_is_busy(bridge, send):
    queue = Queue()
    question = queue.ask("如何继续？", agent="codex")
    text = '使用方案 A；保留 $(echo literal) 和 `literal`\n下一步继续'
    bridge._lock.acquire()
    try:
        send(f"/answer {question['id']} {text}")
    finally:
        bridge._lock.release()
    record = json.loads(queue.path.read_text())["questions"][0]
    assert record["status"] == "answered" and record["answer"] == text
    assert record["actor"] == "feishu:allowed"
    assert "原 Agent 读取后继续" in bridge._send_reply.call_args.args[1]
    bridge._execute_and_reply.assert_not_called()
    assert not bridge._chat_queues


def test_decision_reply_does_not_wait_for_pending_restart(bridge, send):
    question = Queue().ask("如何继续？", agent="codex")
    bridge._restart_pending.set()
    send(f"/answer {question['id']} 同意")
    assert Queue().pending() == []
    assert bridge._restart_pending.is_set()
    bridge._execute_and_reply.assert_not_called()


def test_cancel_marks_only_the_selected_question(bridge, send):
    queue = Queue()
    question = queue.ask("取消这个？", agent="codex")
    other = queue.ask("保留这个", agent="other")
    send(f"/cancel-question {question['id']}")
    assert [item["id"] for item in queue.pending()] == [other["id"]]
    cancelled = json.loads(queue.path.read_text())["questions"][0]
    assert cancelled["status"] == "cancelled" and cancelled["actor"] == "feishu:allowed"
    bridge._execute_and_reply.assert_not_called()


@pytest.mark.parametrize("command", ["/answer", "/answer id", "/answer id   ", "/questions extra", "/cancel-question", "/cancel-question one two"])
def test_empty_or_invalid_arguments_do_not_write_queue(bridge, send, command):
    queue = Queue()
    send(command)
    assert "用法" in bridge._send_reply.call_args.args[1]
    assert not queue.path.exists()
    bridge._execute_and_reply.assert_not_called()


@pytest.mark.parametrize("command", ["/answer missing 方案 A", "/cancel-question missing"])
def test_unknown_question_is_a_friendly_error(bridge, send, command):
    send(command)
    reply = bridge._send_reply.call_args.args[1]
    assert "/questions" in reply and "Traceback" not in reply
    bridge._execute_and_reply.assert_not_called()


@pytest.mark.parametrize("sender,sender_type,group,mention", [
    ("other", "user", False, True),
    ("allowed", "app", False, True),
    ("allowed", "bot", False, True),
    ("allowed", "user", True, False),
])
def test_rejected_senders_and_unmentioned_groups_cannot_answer(bridge, send, sender, sender_type, group, mention):
    queue = Queue()
    question = queue.ask("等待回答", agent="codex")
    before = queue.path.read_bytes()
    send(f"/answer {question['id']} forged", sender=sender, sender_type=sender_type, group=group, mention=mention)
    assert queue.path.read_bytes() == before
    bridge._send_reply.assert_not_called()
    bridge._execute_and_reply.assert_not_called()


def test_duplicate_message_does_not_repeat_decision(bridge, send):
    queue = Queue()
    question = queue.ask("等待回答", agent="codex")
    event = send(f"/answer {question['id']} 方案 A")
    before = queue.path.read_bytes()
    bridge.handle_message(event)
    assert queue.path.read_bytes() == before
    assert bridge._send_reply.call_count == 1


def test_queue_failure_does_not_echo_private_exception(bridge, send, monkeypatch):
    failed = Mock()
    failed.pending.side_effect = OSError("private-token-and-path")
    monkeypatch.setattr(bridge, "DecisionQueue", lambda: failed)
    send("/questions")
    reply = bridge._send_reply.call_args.args[1]
    assert "暂时不可用" in reply and "private-token-and-path" not in reply
    bridge._execute_and_reply.assert_not_called()


def test_help_describes_decision_commands(bridge):
    for command in ("/questions", "/answer", "/cancel-question"):
        assert command in bridge.HELP_TEXT
    assert "只更新本机队列" in bridge.HELP_TEXT
