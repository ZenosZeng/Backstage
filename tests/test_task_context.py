"""Offline task/session continuity without importing native chats or dispatching."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import memory, task_context


NOW = dt.datetime(2026, 10, 27, 12, tzinfo=dt.timezone.utc)


def event(key, *, task="delivery", created="2026-10-27T11:59:00Z", content=None, **changes):
    return memory.normalize_event({
        "schema_version": 1, "event_id": key, "created_at": created,
        "machine_id": "writer", "agent": "codex", "workspace": "test", "projects": [],
        "event_type": "fact", "topic_key": "delivery/continuity", "title": key,
        "task_id": task, "content": {"what": key, "where": ["test evidence"]} | (content or {}),
    } | changes)


def identity(session="session-a", machine="remote", agent="codex", **changes):
    return {"session_id": session, "machine_id": machine, "agent": agent} | changes


def observation(key="running", *, state="running", observed="2026-10-27T11:59:00Z", ref=None, **changes):
    return event(key, content={"agent_runtime": (ref or identity()) | {
        "state": state, "observed_at": observed}}, **changes)


class TaskContextTests(unittest.TestCase):
    def test_legacy_events_remain_valid_and_placeholder_is_not_a_task(self):
        legacy = event("legacy", task="-")
        self.assertEqual(memory.validate_event(legacy), [])
        self.assertEqual(memory.task_states([legacy, event("real")]).keys(), {"delivery"})
        active = event("active", event_type="decision", content={"task_state": "active"})
        self.assertEqual(memory.task_states([active])["delivery"]["state"], "active")
        self.assertEqual(task_context.execution_status([active], "delivery", now=NOW)["state"], "unknown")

    def test_optional_metadata_validation_and_roundtrip(self):
        value = event("metadata", content={"business_projects": ["vla-bridge", "b1k"],
                                           "session_refs": [identity(worktree="/repo/worktree")],
                                           "agent_runtime": identity() | {
                                               "state": "waiting", "observed_at": "2026-10-27T19:59:00+08:00"}})
        self.assertEqual(memory.validate_event(value), [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(memory.append_events(root, [value]), (1, 0))
            self.assertEqual(memory.append_events(root, [value]), (0, 1))
            loaded, errors = memory.load_events(root)
            self.assertEqual(errors, [])
            self.assertEqual(loaded, [value])

    def test_new_metadata_rejects_invalid_fields_before_any_writes(self):
        invalid = [
            {"business_projects": "b1k"}, {"business_projects": ["../b1k"]},
            {"business_projects": ["B1K"]}, {"business_projects": [1]},
            {"session_refs": {}}, {"session_refs": [None]},
            {"session_refs": [identity(agent="unknown")]},
            {"session_refs": [identity(machine="bad/machine")]},
            {"session_refs": [identity(session=" ")]},
            {"session_refs": [identity(session="session\nforeign")]},
            {"session_refs": [identity(worktree="")]},
            {"agent_runtime": []},
            {"agent_runtime": identity() | {"state": "active", "observed_at": "2026-10-27T12:00:00Z"}},
            {"agent_runtime": identity() | {"state": "running", "observed_at": "2026-10-27T12:00:00"}},
            {"agent_runtime": identity() | {"state": "running", "observed_at": 123}},
            {"where": [], "agent_runtime": identity() | {"state": "running", "observed_at": "2026-10-27T12:00:00Z"}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for content in invalid:
                with self.subTest(content=content):
                    self.assertTrue(memory.validate_event(event("invalid", content=content)))
                    with self.assertRaises(ValueError):
                        memory.append_events(root, [event("invalid", content=content)])
                    self.assertEqual(list((root / ".share").rglob("*.json")), [])
        for task in (None, "", "-", "  -  "):
            with self.subTest(task=task):
                self.assertTrue(memory.validate_event(event("missing-task", task=task,
                                                           content={"session_refs": [identity()]})))

    def test_sessions_deduplicate_identity_preserve_provenance_and_isolate_tasks(self):
        events = [event("first", content={"session_refs": [identity(repository="repo-a"), identity()]}),
                  event("second", created="2026-10-27T12:00:00Z", content={"session_refs": [identity(worktree="/wt")]}),
                  event("other-machine", content={"session_refs": [identity(machine="local")]}),
                  event("other-agent", content={"session_refs": [identity(agent="claude")]}),
                  event("other-task", task="elsewhere", content={"session_refs": [identity(session="hidden")]})]
        sources = {e["event_id"]: e["event_id"] + ".json" for e in events}
        with mock.patch("builtins.open", side_effect=AssertionError("do not open native sessions")):
            rows = task_context.session_references(events, "delivery", sources)
        self.assertEqual(len(rows), 3)
        combined = next(r for r in rows if r["machine_id"] == "remote" and r["agent"] == "codex")
        self.assertEqual(combined["event_ids"], ["first", "second"])
        self.assertEqual(combined["repository"], "repo-a")
        self.assertEqual(combined["worktree"], "/wt")
        self.assertEqual(combined["last_seen_at"], "2026-10-27T12:00:00Z")
        self.assertEqual(combined["source"], "second.json")
        self.assertEqual(combined["evidence"][0]["source"], "first.json")

    def test_corrected_and_explicitly_superseded_metadata_is_not_current(self):
        old = observation("old")
        fixed = event("fixed", content={"session_refs": [identity(session="corrected")]}, supersedes=["old"])
        ignored = observation("obsolete", status="superseded", ref=identity(session="obsolete"))
        self.assertEqual([r["session_id"] for r in task_context.session_references([old, fixed, ignored], "delivery")],
                         ["corrected"])
        self.assertEqual(task_context.execution_status([old, fixed, ignored], "delivery", now=NOW)["state"], "unknown")

    def test_runtime_uses_observed_time_not_event_creation_time(self):
        events = [observation("recently-written", observed="2026-10-27T11:50:00Z", created="2026-10-27T12:00:00Z"),
                  observation("latest-observation", state="waiting", observed="2026-10-27T11:59:30Z",
                              created="2026-10-27T11:59:30Z")]
        with mock.patch("builtins.open", side_effect=AssertionError("metadata only")):
            result = task_context.execution_status(events, "delivery", now=NOW,
                                                    sources={"latest-observation": "evidence.json"})
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["age_seconds"], 30)
        self.assertEqual(result["source"], "evidence.json")
        self.assertEqual(result["where"], ["test evidence"])
        self.assertEqual(task_context.session_references(events, "delivery")[0]["session_id"], "session-a")

    def test_runtime_expiry_future_timestamps_and_conflicts_are_unknown(self):
        cases = [
            ([observation(observed="2026-10-27T11:54:59Z")], "stale"),
            ([observation(observed="2026-10-27T12:00:01Z")], "future_observation"),
            ([observation("first"), observation("second", state="idle")], "conflicting_observations"),
        ]
        for events, reason in cases:
            with self.subTest(reason=reason):
                result = task_context.execution_status(events, "delivery", now=NOW)
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(result["reason"], reason)
                self.assertTrue(result["observed_at"])
        boundary = task_context.execution_status([observation(observed="2026-10-27T11:55:00Z")], "delivery", now=NOW)
        self.assertEqual(boundary["state"], "running")
        for invalid in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                task_context.execution_status([], "delivery", now=NOW, fresh_seconds=invalid)

    def test_multiple_sessions_keep_running_and_errors_visible(self):
        events = [observation("working", ref=identity(session="one")),
                  observation("failed", state="error", ref=identity(session="two")),
                  observation("quiet", state="idle", ref=identity(session="three"))]
        result = task_context.execution_status(events, "delivery", now=NOW)
        self.assertEqual(result["state"], "running")
        self.assertEqual({s["state"] for s in result["sessions"]}, {"running", "error", "idle"})


class TaskContextCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config.json").write_text(json.dumps({
            "workspace": "test", "workspace_root": str(self.root), "machine_id": "writer", "projects": {},
        }), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, memory.__file__, "--root", str(self.root), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def add(self, *args):
        return self.cli("add", "--agent", "codex", "--type", "progress", "--topic", "test",
                        "--title", "continuity", "--what", "verified metadata", *args)

    def test_cli_metadata_defaults_cross_machine_references_and_prepared_handoff(self):
        added = self.add("--task", "delivery", "--business-project", "vla-bridge", "--business-project", "vla-bridge",
                         "--session-id", "native-thread", "--runtime-state", "waiting", "--where", "process observation")
        self.assertEqual(added.returncode, 0, added.stderr)
        stored, errors = memory.load_events(self.root)
        self.assertFalse(errors)
        content = stored[0]["content"]
        self.assertEqual(content["business_projects"], ["vla-bridge"])
        self.assertEqual(content["session_refs"], [identity(session="native-thread", machine="writer")])
        self.assertEqual(content["agent_runtime"]["observed_at"], stored[0]["created_at"])
        remote = self.add("--task", "delivery", "--session-id", "remote-thread", "--session-agent", "claude",
                          "--session-machine", "4090maozan", "--session-repository", "repo-a", "--session-worktree", "/repo/wt")
        self.assertEqual(remote.returncode, 0, remote.stderr)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.cli("handoff", "--task", "delivery", "--to-agent", "kimi", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = json.loads(result.stdout)
        self.assertEqual(handoff["handoff"], {"state": "prepared", "to_agent": "kimi", "dispatched": False})
        self.assertEqual(len(handoff["sessions"]), 2)
        self.assertEqual(handoff["execution"]["state"], "waiting")
        text = self.cli("handoff", "--task", "delivery", "--to-agent", "kimi")
        for expected in ("尚未派发", "kimi", "4090maozan/claude/remote-thread", "writer/codex/native-thread", "waiting", "process observation"):
            self.assertIn(expected, text.stdout)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_cli_rejects_incomplete_metadata_without_partial_events(self):
        cases = [
            ("--session-id", "native-thread"),
            ("--task", "-", "--session-id", "native-thread"),
            ("--task", "delivery", "--runtime-state", "running", "--where", "observation"),
            ("--task", "delivery", "--session-id", "native-thread", "--runtime-state", "running"),
            ("--task", "delivery", "--session-agent", "claude"),
            ("--task", "delivery", "--session-id", "native-thread", "--observed-at", "2026-10-27T12:00:00Z"),
            ("--business-project", "../invalid"),
        ]
        for args in cases:
            with self.subTest(args=args):
                result = self.add(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(memory.load_events(self.root), ([], []))

    def test_new_lifecycle_cannot_use_legacy_placeholder_task(self):
        for task in ("-", " - "):
            with self.subTest(task=task):
                result = self.cli("task", "--task", task, "--state", "active", "--agent", "codex",
                                  "--reason", "user started", "--where", "user")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("占位符", result.stderr)
                self.assertEqual(memory.load_events(self.root), ([], []))


if __name__ == "__main__":
    unittest.main()
