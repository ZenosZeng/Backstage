"""A local decision queue and its unattended Feishu reminder monitor."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
import uuid

from tools.config import atomic_json, local_root
from tools.service import Application, lock, main as service_main
from tools.watchdog.notifier import Notifier
from tools.watchdog.runner import run


class Queue:
    """Pending questions shared by local Agents; all mutations take one lock."""

    def __init__(self, path: Path | str | None = None, *, clock=time.time):
        self.path = Path(path) if path is not None else local_root() / "interaction.json"
        self.clock = clock

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": 1, "questions": []}
        except json.JSONDecodeError as exc:
            raise ValueError("interaction queue contains invalid JSON") from exc
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("questions"), list):
            raise ValueError("interaction queue must use schema 1 and a questions list")
        seen = set()
        for item in value["questions"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                or item["id"] in seen
                or not isinstance(item.get("status"), str)
                or item.get("status") not in {"pending", "answered", "cancelled"}
                or not isinstance(item.get("question"), str)
                or not item["question"].strip()
                or not isinstance(item.get("agent"), str)
                or not item["agent"].strip()
                or not _timestamp(item.get("created_at"))
                or (item.get("last_notified") is not None and not _timestamp(item["last_notified"]))
                or type(item.get("notification_count", 0)) is not int
                or item.get("notification_count", 0) < 0
                or not isinstance(item.get("context", ""), str)
                or not isinstance(item.get("task", ""), str)
                or not isinstance(item.get("options", []), list)
                or any(not isinstance(option, str) for option in item.get("options", []))
                or (item.get("answer") is not None and not isinstance(item["answer"], str))
            ):
                raise ValueError("interaction queue contains an invalid or duplicate question")
            # A person can answer by editing the JSON document itself. Treat
            # that response as final without rewriting the file during a read.
            if item["status"] == "pending" and str(item.get("answer") or "").strip():
                item["status"] = "answered"
            seen.add(item["id"])
        return value

    @contextmanager
    def _locked(self):
        # service.lock is deliberately nonblocking for daemon lifecycle calls.
        # Queue writers have very short transactions, so wait briefly for a
        # concurrent Agent instead of dropping its otherwise valid operation.
        deadline = time.monotonic() + 5
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(lock(self.path.with_suffix(".lock")))
                    break
                except ValueError:
                    if time.monotonic() >= deadline:
                        raise ValueError("interaction queue is busy; retry the operation") from None
                    time.sleep(0.01)
            yield

    def pending(self) -> list[dict]:
        """Read only: no directory, cursor or acknowledgement is created."""
        return [item for item in self._read()["questions"] if item["status"] == "pending"]

    def get(self, identifier: str) -> dict:
        """Read one decision, including a recorded answer, without modifying it."""
        item = next((item for item in self._read()["questions"] if item["id"] == identifier), None)
        if item is None:
            raise ValueError(f"unknown question: {identifier}")
        return item

    def ask(self, question: str, *, agent: str, context: str = "", task: str = "", options=None) -> dict:
        for name, value in (("question", question), ("agent", agent)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        if not isinstance(context, str) or not isinstance(task, str):
            raise ValueError("context and task must be strings")
        if options is not None and (
            not isinstance(options, (list, tuple))
            or any(not isinstance(option, str) or not option.strip() for option in options)
        ):
            raise ValueError("options must contain nonempty strings")
        with self._locked():
            value = self._read()
            existing = {item["id"] for item in value["questions"]}
            identifier = "q_" + uuid.uuid4().hex[:12]
            while identifier in existing:
                identifier = "q_" + uuid.uuid4().hex[:12]
            item = {
                "id": identifier,
                "question": question.strip(),
                "agent": agent.strip(),
                "context": context,
                "task": task,
                "options": list(options or []),
                "created_at": self.clock(),
                "status": "pending",
                "last_notified": None,
                "notification_count": 0,
            }
            value["questions"].append(item)
            atomic_json(self.path, value)
            return item

    def _finish(self, identifier: str, *, actor: str, answer: str | None):
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a nonempty string")
        with self._locked():
            value = self._read()
            item = next((item for item in value["questions"] if item["id"] == identifier), None)
            if item is None:
                raise ValueError(f"unknown question: {identifier}")
            if item["status"] != "pending":
                raise ValueError(f"question {identifier} is already {item['status']}")
            item["status"] = "answered" if answer is not None else "cancelled"
            item["updated_at"] = self.clock()
            item["actor"] = actor.strip()
            if answer is not None:
                item["answer"] = answer.strip()
            atomic_json(self.path, value)
            return item

    def answer(self, identifier: str, text: str, *, actor: str = "user") -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("answer must be a nonempty string")
        return self._finish(identifier, actor=actor, answer=text)

    def cancel(self, identifier: str, actor: str = "user") -> dict:
        return self._finish(identifier, actor=actor, answer=None)

    def _acknowledge(self, identifier: str, now: float) -> bool:
        with self._locked():
            value = self._read()
            item = next((item for item in value["questions"] if item["id"] == identifier), None)
            if item is None or item["status"] != "pending":
                return False
            item["last_notified"] = now
            item["notification_count"] = item.get("notification_count", 0) + 1
            atomic_json(self.path, value)
            return True


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def ask(question, *, agent, context="", task="", options=None):
    return Queue().ask(question, agent=agent, context=context, task=task, options=options)


def answer(identifier, text, *, actor="user"):
    return Queue().answer(identifier, text, actor=actor)


def cancel(identifier, actor="user"):
    return Queue().cancel(identifier, actor)


def pending():
    return Queue().pending()


def get(identifier):
    return Queue().get(identifier)


class InteractionMonitor:
    name = "待决策问题"
    alert_interval = 120

    def __init__(self, queue: Queue | None = None, notifier=None, *, clock=time.time):
        self.queue = queue if queue is not None else Queue(clock=clock)
        self.notifier = notifier
        self.clock = clock
        self._attempted_at: dict[str, float] = {}

    def status(self):
        questions = self.queue.pending()
        if not questions:
            return ""
        return "\n\n".join(self._body(item, self.clock(), reminder=False) for item in questions)

    def active(self):
        # Decisions already have their own two-minute reminder cadence.
        # Do not duplicate them with the generic runner's hourly heartbeat.
        return False

    def next_poll_seconds(self):
        """Meet a question's deadline after discovering it between two scans."""
        if self.notifier is None:
            return self.alert_interval
        questions = self.queue.pending()
        if not questions:
            return self.alert_interval
        now = self.clock()
        deadlines = []
        for item in questions:
            baseline = item.get("last_notified")
            baseline = item["created_at"] if baseline is None else baseline
            deadlines.append(max(
                baseline + self.alert_interval,
                self._attempted_at.get(item["id"], float("-inf")) + self.alert_interval,
            ))
        return max(0, min(self.alert_interval, min(deadlines) - now))

    @staticmethod
    def _body(item, now, *, reminder=True):
        lines = [f"**{item['id']}** · {item['question']}", f"- Agent：{item['agent']}"]
        if item.get("context"):
            lines.append(f"- 上下文：{item['context']}")
        if item.get("task"):
            lines.append(f"- Task：{item['task']}")
        if item.get("options"):
            lines.append("- 选项：" + "；".join(item["options"]))
        lines.append(f"- 等待：{max(0, int(now - item['created_at']))} 秒")
        if reminder:
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"- 提醒：第 {item.get('notification_count', 0) + 1} 次 · {stamp}")
        lines.append(f"/answer {item['id']} 你的回答")
        return "\n".join(lines)

    def check_alerts(self):
        if self.notifier is None:
            return []
        questions = self.queue.pending()
        identifiers = {item["id"] for item in questions}
        self._attempted_at = {
            identifier: stamp for identifier, stamp in self._attempted_at.items()
            if identifier in identifiers
        }
        for original in questions:
            # Recheck immediately before delivery: a reply or cancellation since
            # the initial scan must suppress this reminder. Do not hold the queue
            # lock over network IO, so users can always record an answer quickly.
            item = next((item for item in self.queue.pending() if item["id"] == original["id"]), None)
            if item is None:
                continue
            now = self.clock()
            baseline = item.get("last_notified")
            baseline = item["created_at"] if baseline is None else baseline
            if now - baseline < self.alert_interval:
                continue
            if now - self._attempted_at.get(item["id"], float("-inf")) < self.alert_interval:
                continue
            body = self._body(item, now)
            self._attempted_at[item["id"]] = now
            if self.notifier.send_card("warning", "等待用户决策", body):
                # If a response arrives while sending, preserve that response.
                self.queue._acknowledge(item["id"], now)
                self._attempted_at.pop(item["id"], None)
        return []


def factory(config: dict, config_path: Path, directory: Path) -> Application:
    poll = config.get("poll_seconds", 120)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or poll <= 0:
        raise ValueError("poll_seconds must be finite and positive")
    notifier = Notifier(config, config_path.parent, "交互问题")
    monitor = InteractionMonitor(Queue(), notifier)
    return Application(
        run=lambda stop: run(monitor, notifier, stop, poll_seconds=poll),
        snapshot=lambda: {"monitor": monitor.name, "status": monitor.status(), "pending": monitor.queue.pending()},
    )


def entry(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["watch"]:
        return service_main("interaction", ["tools.watchdog.interaction", "watch"], factory, arguments[1:])
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    create = commands.add_parser("ask", help="append a pending decision")
    create.add_argument("--question", required=True)
    create.add_argument("--agent", default="agent")
    create.add_argument("--context", default="")
    create.add_argument("--task", default="")
    create.add_argument("--option", action="append", default=[])
    reply = commands.add_parser("answer", help="record a user's answer")
    reply.add_argument("id")
    reply.add_argument("text")
    reply.add_argument("--actor", default="user")
    dismiss = commands.add_parser("cancel", help="cancel a pending decision")
    dismiss.add_argument("id")
    dismiss.add_argument("--actor", default="user")
    commands.add_parser("list", help="list pending decisions without changing state")
    show = commands.add_parser("show", help="read a decision and its answer by ID")
    show.add_argument("id")
    commands.add_parser("watch", help="run the reminder service; accepts service options")
    args = cli.parse_args(arguments)
    try:
        if args.command == "ask":
            result = ask(args.question, agent=args.agent, context=args.context, task=args.task, options=args.option)
        elif args.command == "answer":
            result = answer(args.id, args.text, actor=args.actor)
        elif args.command == "cancel":
            result = cancel(args.id, actor=args.actor)
        elif args.command == "show":
            result = get(args.id)
        else:
            result = pending()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"interaction: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(entry())
