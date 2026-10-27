"""Semantic review preparation and result validation need only the stdlib."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.memory_review import MAX_FINDINGS, prepare_review, validate_findings


def event(key, hour=0, **changes):
    return dict(schema_version=1, event_id=key, created_at=f"2026-10-27T{hour:02}:00:00Z",
                machine_id="fixture", agent="codex", workspace="demo",
                projects=[{"name": "repo-a"}], event_type="fact", topic_key="contract",
                task_id="task-a", title=key, content={"what": "原文\n保持空白", "where": ["fixture"]},
                status="active", supersedes=[]) | changes


def sources(events):
    return {e["event_id"]: f".share/memory/fixture/codex/{e['event_id']}.json" for e in events}


def compact(report):
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"))


class MemoryReviewTests(unittest.TestCase):
    def test_direct_cli_prepares_scoped_packet_and_validates_findings_without_writes(self):
        from scripts import memory
        script = Path(memory.__file__).resolve()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".agents"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"workspace": "demo", "machine_id": "fixture",
                                                         "workspace_root": temp, "projects": {}}))
            memory.append_events(root, [event("proof")])
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            args = [sys.executable, str(script), "--root", str(root), "review"]
            missing = subprocess.run(args + ["--json"], cwd=temp, capture_output=True, text=True)
            self.assertEqual(missing.returncode, 2)
            result = subprocess.run(args + ["--task", "task-a", "--json"], cwd=temp,
                                    capture_output=True, text=True, check=True)
            packet = json.loads(result.stdout)
            self.assertEqual(packet["state"], "prepared")
            self.assertEqual(len(result.stdout.rstrip("\n")), packet["budget"]["used_chars"])
            findings = Path(temp) / "findings.json"
            findings.write_text(json.dumps({"findings": [{"category": "outdated", "event_ids": ["proof"],
                                                          "summary": "Verify against current source"}]}))
            checked = subprocess.run(args + ["--task", "task-a", "--input-findings", str(findings), "--json"],
                                     cwd=temp, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(checked.stdout)["state"], "needs_verification")
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_scope_is_required_and_empty_or_invalid_filters_are_rejected(self):
        for kwargs in ({}, {"task": ""}, {"project": " "}, {"task": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                prepare_review([], {}, **kwargs)

    def test_task_project_and_combined_scope_exclude_unrelated_evidence(self):
        events = [event("in"), event("cross-repo", projects=[{"name": "repo-b"}]),
                  event("other-task", task_id="task-b"),
                  event("neither", task_id="task-b", projects=[{"name": "repo-b"}])]
        for kwargs, expected in (({"task": "task-a"}, {"in", "cross-repo"}),
                                 ({"project": "repo-a"}, {"in", "other-task"}),
                                 ({"task": "task-a", "project": "repo-a"}, {"in"})):
            with self.subTest(kwargs=kwargs):
                result = prepare_review(events, sources(events), **kwargs)
                self.assertEqual({i["event_id"] for i in result["candidates"]}, expected)
                self.assertEqual(result["state"], "prepared")
                self.assertEqual(result["omitted"], 0)
                self.assertIn("未执行审查", result["instructions"])

    def test_explicit_replacement_chain_and_status_are_excluded_globally(self):
        events = [event("old"), event("middle", supersedes=["old"]),
                  event("new", supersedes=["middle"]), event("marked", status="superseded"),
                  event("replaced-elsewhere"),
                  event("outside", task_id="other", projects=[{"name": "repo-b"}],
                        supersedes=["replaced-elsewhere"])]
        result = prepare_review(events, sources(events), task="task-a")
        self.assertEqual([i["event_id"] for i in result["candidates"]], ["new"])
        self.assertEqual(result["scope_total"], 5)
        self.assertEqual(result["excluded_superseded"], 4)

    def test_parallel_unresolved_heads_are_not_resolved_by_recency(self):
        events = [event("base"), event("left", 1, supersedes=["base"]),
                  event("right", 2, supersedes=["base"])]
        result = prepare_review(events, sources(events), task="task-a")
        self.assertEqual([i["event_id"] for i in result["candidates"]], ["right", "left"])
        self.assertEqual(result["omitted"], 0)

    def test_cycle_is_rejected_instead_of_dropping_all_its_records(self):
        events = [event("a", supersedes=["b"]), event("b", supersedes=["a"])]
        with self.assertRaisesRegex(ValueError, "环"):
            prepare_review(events, sources(events), task="task-a")

    def test_order_uses_actual_timestamps(self):
        events = [event("local", created_at="2026-10-27T08:30:00+08:00"), event("utc", 1)]
        result = prepare_review(events, sources(events), task="task-a", limit=1)
        self.assertEqual(result["candidates"][0]["event_id"], "utc")
        self.assertEqual(result["omitted"], 1)

    def test_whole_serialized_report_is_bounded_and_truncation_is_explicit(self):
        events = [event(str(i), i, title="长标题"*300,
                        content={"what": "原文\n证据"*1000, "where": ["fixture"]}) for i in range(15)]
        for budget in (1600, 2500, 5000, 12000):
            with self.subTest(budget=budget):
                result = prepare_review(events, sources(events), task="task-a", limit=5, max_chars=budget)
                self.assertLessEqual(len(compact(result)), budget)
                self.assertEqual(result["budget"]["used_chars"], len(compact(result)))
                self.assertLessEqual(len(result["candidates"]), 5)
                self.assertEqual(result["omitted"], 15-len(result["candidates"]))
                for item in result["candidates"]:
                    self.assertEqual(item["source"], sources(events)[item["event_id"]])
                    self.assertGreater(item["title_chars_omitted"], 0)
                    self.assertGreater(item["excerpt_chars_omitted"], 0)

    def test_identity_too_large_is_omitted_whole_and_no_other_scope_leaks(self):
        key = "evt_" + "x"*3000
        events = [event(key), event("other", task_id="unrelated", content={"what": "PRIVATE_OTHER_SCOPE"})]
        result = prepare_review(events, sources(events), task="task-a", max_chars=1600)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["omitted"], 1)
        self.assertNotIn("evt_", compact(result))
        self.assertNotIn("PRIVATE_OTHER_SCOPE", compact(result))

    def test_small_budget_and_bad_numeric_limits_are_errors(self):
        for kwargs in ({"max_chars": 1}, {"max_chars": 0}, {"max_chars": True},
                       {"limit": 0}, {"limit": 51}, {"limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                prepare_review([], {}, task="task-a", **kwargs)

    def test_missing_source_is_explicit_and_original_excerpt_is_preserved(self):
        item = prepare_review([event("one")], {}, task="task-a")["candidates"][0]
        self.assertIsNone(item["source"])
        self.assertIn("原文\n保持空白", item["excerpt"])
        self.assertEqual(item["excerpt_chars_omitted"], 0)

    def test_prepare_and_validate_do_not_modify_the_input(self):
        events = [event("one"), event("two")]
        paths = sources(events)
        before_events, before_paths = copy.deepcopy(events), copy.deepcopy(paths)
        report = prepare_review(events, paths, task="task-a")
        before_report = copy.deepcopy(report)
        findings = [{"category": "duplicate", "event_ids": ["one", "two"], "summary": "需要核对"}]
        before_findings = copy.deepcopy(findings)
        result = validate_findings(report, findings)
        self.assertEqual(events, before_events)
        self.assertEqual(paths, before_paths)
        self.assertEqual(report, before_report)
        self.assertEqual(findings, before_findings)
        self.assertEqual(result["state"], "needs_verification")
        self.assertEqual(result["findings"][0]["status"], "needs_verification")
        self.assertEqual(result["findings"][0]["sources"][0]["source"], paths["one"])

    def test_only_packet_ids_are_accepted_including_omitted_and_replaced_ids(self):
        events = [event("old"), event("new", 1, supersedes=["old"]), event("latest", 2)]
        report = prepare_review(events, sources(events), task="task-a", limit=1)
        for key in ("unknown", "old", "new"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "包"):
                validate_findings(report, [{"category": "outdated", "event_ids": [key], "summary": "检查"}])

    def test_all_categories_are_candidates_not_verified_conclusions(self):
        report = prepare_review([event("one"), event("two")], {}, task="task-a")
        findings = [{"category": category, "event_ids": ["one", "one", "two"], "summary": "待核验"}
                    for category in ("contradiction", "outdated", "duplicate")]
        result = validate_findings(report, {"findings": findings})
        self.assertEqual(len(result["findings"]), 3)
        self.assertTrue(all(i["status"] == "needs_verification" for i in result["findings"]))
        self.assertEqual(result["findings"][0]["event_ids"], ["one", "two"])
        self.assertEqual(validate_findings(report, [])['state'], "needs_verification")

    def test_malformed_or_oversized_findings_are_rejected(self):
        report = prepare_review([event("one")], {}, task="task-a")
        valid = {"category": "outdated", "event_ids": ["one"], "summary": "核对"}
        malformed = [None, "text", {"findings": [], "state": "verified"},
                     [valid]* (MAX_FINDINGS+1), [valid | {"summary": "x"*2001}],
                     [valid | {"summary": " "}], [valid | {"event_ids": []}],
                     [valid | {"event_ids": ["one"]*21}], [valid | {"category": "delete"}],
                     [valid | {"status": "verified"}], [valid | {"event_ids": [{}]}]]
        for value in malformed:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                validate_findings(report, value)
        with self.assertRaises(ValueError):
            validate_findings({"state": "verified", "candidates": []}, [])


if __name__ == "__main__":
    unittest.main()
