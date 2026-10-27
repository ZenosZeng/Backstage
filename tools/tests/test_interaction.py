"""Local decision queue and reminder behavior, without live Feishu delivery."""

from concurrent.futures import ThreadPoolExecutor
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.config import atomic_json
from tools.watchdog import interaction
from tools.watchdog import runner
from tools.watchdog.notifier import Notifier


@pytest.fixture
def clock():
    return SimpleNamespace(now=1000)


@pytest.fixture
def queue(tmp_path, clock):
    return interaction.Queue(tmp_path / "interaction.json", clock=lambda: clock.now)


def _monitor(queue, clock, sent, *, delivered=True):
    notifier = SimpleNamespace(send_card=lambda *args: sent.append(args) or delivered)
    return interaction.InteractionMonitor(queue, notifier, clock=lambda: clock.now)


def test_question_answer_and_cancellation_preserve_identity_and_metadata(queue, clock):
    first = queue.ask("选哪个方案？", agent="codex", context="等待部署", task="infra", options=["A", "B"])
    second = queue.ask("继续吗？", agent="claude")
    assert first["id"] != second["id"]
    assert first["created_at"] == 1000
    assert first["status"] == "pending" and first["options"] == ["A", "B"]
    clock.now += 30
    answered = queue.answer(first["id"], "方案 A", actor="user")
    assert answered["status"] == "answered" and answered["answer"] == "方案 A"
    assert answered["actor"] == "user" and answered["updated_at"] == 1030
    cancelled = queue.cancel(second["id"], "codex")
    assert cancelled["status"] == "cancelled" and cancelled["actor"] == "codex"
    assert queue.pending() == []
    assert queue.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already answered"):
        queue.answer(first["id"], "other")
    with pytest.raises(ValueError, match="already cancelled"):
        queue.answer(second["id"], "yes")


def test_multiple_agents_append_and_answer_without_lost_updates(queue):
    def ask(index):
        return interaction.Queue(queue.path).ask(f"问题 {index}", agent=f"agent-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        questions = list(pool.map(ask, range(24)))
    assert len(queue.pending()) == 24
    assert len({item["id"] for item in questions}) == 24

    def finish(question):
        return interaction.Queue(queue.path).answer(question["id"], "继续", actor="user")

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(finish, questions))
    assert len(replies) == 24 and queue.pending() == []
    records = json.loads(queue.path.read_text())["questions"]
    assert len(records) == 24 and all(item["answer"] == "继续" for item in records)


def test_status_and_empty_pending_do_not_create_or_change_files(queue, clock):
    monitor = _monitor(queue, clock, [])
    assert queue.pending() == [] and monitor.status() == ""
    assert not queue.path.exists()
    assert not queue.path.with_suffix(".lock").exists()
    question = queue.ask("确认发布？", agent="codex")
    before = queue.path.read_bytes()
    clock.now += 300
    assert question["id"] in monitor.status()
    assert queue.path.read_bytes() == before


def test_get_reads_answered_or_cancelled_questions_without_changing_document(queue):
    answered = queue.ask("一个问题", agent="agent")
    cancelled = queue.ask("另一个问题", agent="agent")
    queue.answer(answered["id"], "回答")
    queue.cancel(cancelled["id"])
    before = queue.path.read_bytes()
    assert queue.get(answered["id"])["answer"] == "回答"
    assert queue.get(cancelled["id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="unknown question"):
        queue.get("unknown")
    assert queue.path.read_bytes() == before


def test_first_reminder_after_two_minutes_repeats_until_answered(queue, clock):
    question = queue.ask("是否部署？", agent="codex", context="PR 已通过", options=["部署", "等待"])
    sent = []
    monitor = _monitor(queue, clock, sent)
    clock.now = 1119
    assert monitor.check_alerts() == [] and sent == []
    clock.now = 1120
    assert monitor.check_alerts() == [] and len(sent) == 1
    assert f"/answer {question['id']} 你的回答" in sent[0][2]
    assert "codex" in sent[0][2] and "PR 已通过" in sent[0][2]
    assert "第 1 次" in sent[0][2]
    clock.now = 1239
    monitor.check_alerts()
    assert len(sent) == 1
    clock.now = 1240
    monitor.check_alerts()
    assert len(sent) == 2 and "第 2 次" in sent[1][2]
    queue.answer(question["id"], "部署")
    clock.now = 1360
    monitor.check_alerts()
    assert len(sent) == 2


def test_hour_boundary_keeps_lifecycle_and_due_reminder_without_extra_heartbeat(queue, clock, monkeypatch):
    queue.ask("继续吗？", agent="agent")
    clock.now = 1120
    sent = []
    monitor = _monitor(queue, clock, sent)
    monkeypatch.setattr(runner.time, "localtime", lambda: time.struct_time((2026, 10, 27, 12, 0, 0, 1, 300, 0)))

    class Stop:
        done = False

        def is_set(self):
            return self.done

        def wait(self, _):
            self.done = True

    assert runner.run(monitor, monitor.notifier, Stop(), poll_seconds=120) == 0
    assert [values[0] for values in sent] == ["watchdog_started", "warning", "watchdog_stopped"]


def test_poll_phase_does_not_delay_first_reminder_to_four_minutes(queue, clock):
    clock.now = 0
    sent = []
    monitor = _monitor(queue, clock, sent)
    delays = []

    class Stop:
        done = False

        def is_set(self):
            return self.done

        def wait(self, delay):
            delays.append(delay)
            if len(delays) == 1:
                # The question arrives one second after the initially empty
                # scan, while the service is sleeping until t=120.
                clock.now = 1
                queue.ask("是否继续？", agent="agent")
                clock.now = 120
            elif len(delays) == 2:
                clock.now += delay
            else:
                self.done = True

    assert runner.run(monitor, monitor.notifier, Stop(), poll_seconds=120, quiet_hours=[]) == 0
    reminders = [values for values in sent if values[0] == "warning"]
    assert delays[:2] == [120, 1]
    assert len(reminders) == 1
    assert "等待：120 秒" in reminders[0][2]
    assert queue.pending()[0]["last_notified"] == 121


def test_failed_delivery_retry_deadline_does_not_spin_or_drift_with_other_questions(queue, clock):
    first = queue.ask("第一个问题", agent="agent")
    sent = []
    monitor = _monitor(queue, clock, sent, delivered=False)
    clock.now = 1120
    monitor.check_alerts()
    assert len(sent) == 1 and monitor.next_poll_seconds() == 120
    clock.now = 1130
    queue.ask("第二个问题", agent="agent")
    monitor.check_alerts()
    assert len(sent) == 1 and monitor.next_poll_seconds() == 110
    clock.now = 1240
    monitor.check_alerts()
    assert len(sent) == 2 and first["id"] in sent[1][2]
    assert monitor.next_poll_seconds() == 10  # second question is due at 1250
    assert queue.pending()[0]["last_notified"] is None


def test_failed_delivery_is_retried_without_advancing_notification_ack(queue, clock):
    question = queue.ask("继续？", agent="agent")
    clock.now = 1120
    sent = []
    monitor = _monitor(queue, clock, sent, delivered=False)
    before = queue.path.read_bytes()
    monitor.check_alerts()
    assert len(sent) == 1 and queue.path.read_bytes() == before
    resumed = _monitor(interaction.Queue(queue.path), clock, sent)
    resumed.check_alerts()
    record = queue.pending()[0]
    assert record["id"] == question["id"] and record["last_notified"] == 1120
    assert record["notification_count"] == 1
    resumed.check_alerts()
    assert len(sent) == 2


def test_real_notifier_dedup_does_not_swallow_two_minute_reminders(queue, clock, monkeypatch):
    queue.ask("发布吗？", agent="codex")
    monkeypatch.setattr("tools.watchdog.notifier.legacy_value", lambda key: "test-key")
    notifier = Notifier({}, queue.path.parent, "测试")
    monitor = interaction.InteractionMonitor(queue, notifier, clock=lambda: clock.now)
    response = SimpleNamespace(read=lambda: b'{"code":0}')
    with patch("tools.watchdog.notifier.urllib.request.urlopen") as post:
        post.return_value.__enter__.return_value = response
        clock.now = 1120
        monitor.check_alerts()
        clock.now = 1240
        monitor.check_alerts()
        assert post.call_count == 2
    assert queue.pending()[0]["notification_count"] == 2


def test_reply_between_initial_scan_and_pre_delivery_recheck_suppresses_card(queue, clock, monkeypatch):
    question = queue.ask("是否继续？", agent="agent")
    read = queue.pending
    calls = 0

    def pending():
        nonlocal calls
        calls += 1
        if calls == 2:
            queue.answer(question["id"], "继续")
        return read()

    monkeypatch.setattr(queue, "pending", pending)
    sent = []
    clock.now = 1120
    _monitor(queue, clock, sent).check_alerts()
    assert sent == []


def test_reply_during_delivery_is_not_overwritten_by_notification_ack(queue, clock):
    question = queue.ask("是否继续？", agent="agent")

    def send(*args):
        queue.answer(question["id"], "继续", actor="user")
        return True

    monitor = interaction.InteractionMonitor(queue, SimpleNamespace(send_card=send), clock=lambda: clock.now)
    clock.now = 1120
    monitor.check_alerts()
    record = json.loads(queue.path.read_text())["questions"][0]
    assert record["status"] == "answered" and record["answer"] == "继续"
    assert record["last_notified"] is None


def test_manual_answer_stops_reminders_without_requiring_status_edit(queue, clock):
    question = queue.ask("哪一个？", agent="agent")
    value = json.loads(queue.path.read_text())
    value["questions"][0]["answer"] = "第二个"
    atomic_json(queue.path, value)
    before = queue.path.read_bytes()
    sent = []
    clock.now = 1120
    monitor = _monitor(queue, clock, sent)
    assert queue.pending() == [] and monitor.status() == ""
    monitor.check_alerts()
    assert sent == [] and queue.path.read_bytes() == before
    with pytest.raises(ValueError, match="already answered"):
        queue.answer(question["id"], "第一个")


@pytest.mark.parametrize("value", ["not json", '{}', '{"schema":1,"questions":{}}'])
def test_invalid_queue_is_rejected_without_overwrite(queue, value):
    queue.path.write_text(value)
    with pytest.raises(ValueError):
        queue.ask("question", agent="agent")
    assert queue.path.read_text() == value


def test_duplicate_question_ids_are_rejected_without_overwrite(queue):
    question = queue.ask("question", agent="agent")
    value = json.loads(queue.path.read_text())
    value["questions"].append(question)
    atomic_json(queue.path, value)
    before = queue.path.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        queue.answer(question["id"], "answer")
    assert queue.path.read_bytes() == before


def test_cli_ask_list_answer_cancel_use_same_default_document(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_LOCAL_ROOT", str(tmp_path))
    assert interaction.entry(["ask", "--question", "继续吗？", "--agent", "codex", "--option", "继续"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert interaction.entry(["list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == first["id"]
    assert interaction.entry(["answer", first["id"], "继续", "--actor", "user"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "answered"
    assert interaction.entry(["show", first["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["answer"] == "继续"
    assert interaction.entry(["ask", "--question", "等待？"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert interaction.entry(["cancel", second["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert interaction.entry(["answer", "unknown", "yes"]) == 2
    assert "unknown question" in capsys.readouterr().err


def test_watch_cli_delegates_lifecycle_to_existing_service(monkeypatch):
    values = []
    monkeypatch.setattr(interaction, "service_main", lambda *args: values.append(args) or 0)
    assert interaction.entry(["watch", "--once", "--no-notify"]) == 0
    assert values[0][0] == "interaction"
    assert values[0][1] == ["tools.watchdog.interaction", "watch"]
    assert values[0][2] is interaction.factory
    assert values[0][3] == ["--once", "--no-notify"]


def test_factory_snapshot_is_read_only_and_uses_shared_default_document(queue, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_LOCAL_ROOT", str(tmp_path))
    queue.ask("问题？", agent="agent")
    before = queue.path.read_bytes()
    app = interaction.factory(
        {"notify": False},
        tmp_path / "config.json", tmp_path / "runtime",
    )
    result = app.snapshot()
    assert len(result["pending"]) == 1
    assert queue.path.read_bytes() == before
