"""Offline progressive recall: bounded index, correction history, batch evidence."""
import json
import subprocess
import sys

import pytest

from scripts import memory


def event(key, hour=0, **changes):
    return dict(schema_version=1, event_id=key, created_at=f"2026-10-22T{hour:02}:00:00Z",
                machine_id="test", agent="codex", workspace="demo", projects=[{"name": "one"}],
                event_type="fact", topic_key="test", title=key, content={"what": "evidence", "where": ["source"]},
                status="active", supersedes=[]) | changes


def test_context_cross_repo_task_neighbors_corrections_and_closed_state():
    events = [event("old", task_id="task"),
              event("new", 1, task_id="task", supersedes=["old"], projects=[{"name": "two"}]),
              event("next", 2, task_id="task"), event("unrelated", 1, task_id="other"),
              event("closed", 3, task_id="task", event_type="decision",
                    content={"what": "user accepted", "where": ["user"], "task_state": "closed"})]
    sources = {e["event_id"]: e["event_id"]+".json" for e in events}
    result = memory.event_context(events, sources, "old", before=0, after=1)
    assert result["task_state"] == "closed"
    assert [e["event_id"] for e in result["items"]] == ["old", "new"]
    assert result["items"][0]["replaced_by"] == ["new"]
    assert result["items"][1]["correction"]
    assert result["omitted_neighbors"] == 2
    assert "授权" in result["notice"]


def test_no_task_context_is_scoped_by_topic_projects_and_workspace():
    events = [event("anchor"), event("same", 1), event("other-topic", topic_key="other"),
              event("other-project", projects=[{"name": "two"}]), event("other-workspace", workspace="other"),
              event("has-task", task_id="task")]
    result = memory.event_context(events, {e["event_id"]: "test.json" for e in events}, "anchor")
    assert {e["event_id"] for e in result["items"]} == {"anchor", "same"}
    with pytest.raises(FileNotFoundError):
        memory.event_context(events, {}, "absent")
    with pytest.raises(ValueError):
        memory.event_context(events, {}, "anchor", before=-1)


def test_context_limits_correction_chain_and_orders_by_actual_time():
    events = [event(str(i), task_id="task", supersedes=[str(i-1)] if i else []) for i in range(100)]
    result = memory.event_context(events, {e["event_id"]: "test.json" for e in events}, "0")
    assert len(result["items"]) == 20
    assert result["corrections_truncated"] is True
    events = [event("first", created_at="2026-10-22T08:00:00+08:00"), event("second", 1)]
    result = memory.event_context(events, {e["event_id"]: "test.json" for e in events}, "second", after=0)
    assert [e["event_id"] for e in result["items"]] == ["first", "second"]


def test_cli_context_budget_batch_get_and_legacy_single_get(tmp_path):
    memory.append_events(tmp_path, [event("old"), event("new", 1, supersedes=["old"])])
    def run(*args):
        return subprocess.run([sys.executable, memory.__file__, "--root", str(tmp_path), *args],
                              text=True, capture_output=True)
    single = json.loads(run("get", "old").stdout)
    assert set(single) == {"source", "event"}
    batch = json.loads(run("get", "new", "old", "new").stdout)
    assert [e["event"]["event_id"] for e in batch["items"]] == ["new", "old"]
    partial = run("get", "old", "absent")
    assert partial.returncode == 1 and json.loads(partial.stdout)["missing"] == ["absent"]
    assert run("get", *(str(i) for i in range(21))).returncode == 2
    assert len(run("context", "old", "--max-chars", "300").stdout.rstrip()) <= 300
    assert json.loads(run("context", "old", "--json").stdout)["anchor_id"] == "old"
    assert run("context", "absent").returncode != 0


def test_cli_context_reports_corrections_omitted_by_text_budget(tmp_path):
    memory.append_events(tmp_path, [event("old", content={"what": "evidence " * 20}),
                                    event("new", 1, supersedes=["old"],
                                          content={"what": "evidence " * 20})])
    def context(budget):
        result = subprocess.run([sys.executable, memory.__file__, "--root", str(tmp_path),
                                 "context", "old", "--before", "0", "--after", "0",
                                 "--max-chars", str(budget)], text=True, capture_output=True)
        assert result.returncode == 0
        assert len(result.stdout.rstrip()) <= budget
        return result.stdout
    anchor_only = context(600)
    assert "来源: old ·" in anchor_only and "来源: new ·" not in anchor_only
    assert "修正链有省略" in anchor_only and "修正链已列出" not in anchor_only
    neither = context(220)
    assert "来源:" not in neither and "修正链有省略" in neither
    assert "预算不足" in context(20)
    complete = context(900)
    assert "来源: new ·" in complete and "修正链已列出" in complete


def test_cli_context_gives_corrections_budget_before_ordinary_neighbors(tmp_path):
    long_content = {"what": "细节" * 120}
    memory.append_events(tmp_path, [event("anchor", content=long_content),
                                    event("neighbor", 1, content=long_content),
                                    event("correction", 2, supersedes=["anchor"], content=long_content)])
    result = subprocess.run([sys.executable, memory.__file__, "--root", str(tmp_path),
                             "context", "anchor", "--before", "0", "--after", "1",
                             "--max-chars", "900"], text=True, capture_output=True)
    assert result.returncode == 0 and len(result.stdout.rstrip()) <= 900
    assert "来源: anchor ·" in result.stdout and "来源: correction ·" in result.stdout
    assert "来源: neighbor ·" not in result.stdout
    assert "修正链已列出" in result.stdout and "未展示邻域/预算记录 1" in result.stdout


def test_linear_cycle_detection_keeps_only_cycle_members_on_deep_graph():
    events = [{"event_id": str(i), "supersedes": [str(i+1)]} for i in range(10000)]
    assert memory.supersedes_cycles(events) == set()
    events[-1]["supersedes"] = ["9997"]
    assert memory.supersedes_cycles(events) == {"9997", "9998", "9999"}
    events.append({"event_id": "self", "supersedes": ["self"]})
    assert "self" in memory.supersedes_cycles(events)


def test_context_exposes_correction_cycles_without_claiming_task_state():
    events = [event("a", task_id="task", supersedes=["b"]), event("b", task_id="task", supersedes=["a"])]
    result = memory.event_context(events, {"a": "test.json", "b": "test.json"}, "a")
    assert result["task_state"] == "unknown" and result["warnings"]
    assert {e["event_id"] for e in result["items"]} == {"a", "b"}
