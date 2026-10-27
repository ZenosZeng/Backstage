"""Task context uses explicit evidence and one snapshot for journey statistics."""
import json
from datetime import datetime, timezone
import threading
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts import memory
from tools.web.__main__ import Server
from tools.web.data import Workspace


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / ".agents"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({
        "workspace": "journeys", "workspace_root": str(tmp_path), "machine_id": "host-a",
        "projects": {"repo": {"path": "repo"}},
    }))
    directory = root / ".share/knowledge/projects"
    directory.mkdir(parents=True)
    for name in ("alpha", "beta"):
        (directory / f"{name}.md").write_text(f"# {name}\n\nCurated project.\n")
    return Workspace(root, root / ".local", ttl=0)


def record(key, task="shared", business=None, **updates):
    content = {"what": "Confirmed outcome", "where": ["source evidence"]}
    if business is not None:
        content["business_projects"] = business
    return {"schema_version": 1, "event_id": key, "created_at": "2026-10-01T00:00:00Z",
            "machine_id": "host-a", "agent": "codex", "workspace": "journeys",
            "projects": [{"name": "repo"}], "event_type": "fact", "topic_key": "generic",
            "title": key, "content": content, "status": "active", "task_id": task, **updates}


def test_timeline_deduplicates_shared_tasks_and_reports_unassigned(workspace):
    memory.append_events(workspace.root, [
        record("start", business=["alpha", "beta"], event_type="decision",
               content={"what": "User started", "where": ["request"], "task_state": "active",
                        "business_projects": ["alpha", "beta"]}),
        record("unassigned", task="other"), record("placeholder", task="-"),
    ])
    data = workspace.timeline()
    assert data["task_count"] == 1
    assert data["task_counts"]["active"] == 1
    assert sum(p["task_count"] for p in data["projects"]) == 2
    assert data["coverage"]["unassigned_tasks"] == 1
    assert data["coverage"]["unassigned_events"] == 2
    assert data["coverage"]["unassigned_task_items"] == [{"task_id": "other", "state": "unknown"}]


def test_explicit_membership_overrides_prefix_and_reports_missing_projects(workspace):
    memory.append_events(workspace.root, [
        record("mapped", task="alpha-build", business=["beta"]),
        record("none", task="alpha-none", business=[]),
        record("missing", task="alpha-missing", business=["not-organized"]),
    ])
    data = workspace.timeline()
    assert next(p for p in data["projects"] if p["name"] == "alpha")["event_count"] == 0
    assert next(p for p in data["projects"] if p["name"] == "beta")["event_count"] == 1
    assert data["coverage"]["unassigned_events"] == 2
    assert any("not-organized" in error for error in data["errors"])


def test_detail_shows_sessions_results_and_unknown_execution_without_inference(workspace):
    memory.append_events(workspace.root, [
        record("first", task="alpha-build", business=["alpha"], content={
            "what": "Decision", "where": ["review"], "business_projects": ["alpha"],
            "session_refs": [{"agent": "codex", "machine_id": "host-a", "session_id": "thread-one"}],
        }, event_type="decision"),
        record("result", task="alpha-build", business=["alpha"], event_type="test_result"),
        record("second", task="alpha-build", business=["alpha"], content={
            "what": "Continuation", "session_refs": [{"agent": "claude", "machine_id": "host-b",
                                                        "session_id": "thread-two"}],
        }),
    ])
    response = workspace.timeline("alpha", "alpha-build")
    detail = response["task_detail"]
    assert {s["session_id"] for s in detail["sessions"]} == {"thread-one", "thread-two"}
    assert detail["execution"]["state"] == "unknown"
    assert [e["event_id"] for e in detail["results"]] == ["result"]
    assert detail["sessions"][0]["source"].startswith(".share/memory/")
    assert "session_refs" not in json.dumps(detail)


def test_attention_uses_fresh_waiting_evidence_not_lifecycle_or_old_risks(workspace):
    now = datetime.now(timezone.utc).isoformat()
    memory.append_events(workspace.root, [record("wait", task="alpha-build", business=["alpha"], content={
        "what": "Waiting on explicit decision", "where": ["host observation"],
        "agent_runtime": {"agent": "codex", "machine_id": "host-a", "session_id": "thread",
                          "state": "waiting", "observed_at": now},
    }), record("risk", task="alpha-paused", event_type="risk"), record(
        "pause", task="alpha-paused", event_type="decision", content={
            "what": "User paused", "where": ["user"], "task_state": "paused"}),
    ])
    data = workspace.timeline()
    assert data["attention_count"] == 1
    assert data["attention"][0]["task_id"] == "alpha-build"
    assert data["attention"][0]["execution"]["event_id"] == "wait"


def test_statistics_refresh_without_reading_old_overview(workspace):
    memory.append_events(workspace.root, [record("first", task="alpha-build")])
    old = workspace.overview()
    memory.append_events(workspace.root, [record("start", task="alpha-build", event_type="decision", content={
        "what": "User starts task", "where": ["user"], "task_state": "active"})])
    fresh = workspace.timeline()
    assert old["counts"]["active_tasks"] == 0
    assert fresh["task_counts"]["active"] == 1


def test_direct_document_preview_rechecks_expired_provenance(workspace):
    repo = workspace.root.parent / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "api.py").write_text("version = 1\n")
    git("add", "api.py")
    git("commit", "-qm", "fixture")
    metadata = {"events": [], "knowledge": {"verified_at": "2026-09-01T00:00:00Z", "sources": [
        {"repository": "repo", "commit": git("rev-parse", "HEAD"), "covers": ["api.py"]}]}}
    (workspace.root / ".share/knowledge/projects/alpha.json").write_text(json.dumps(metadata))
    now = [0]
    workspace = Workspace(workspace.root, workspace.local, ttl=3, clock=lambda: now[0])
    workspace.overview()
    path = ".share/knowledge/projects/alpha.md"
    assert workspace.document(path)["freshness"]["state"] == "current"
    memory.append_events(workspace.root, [record("new-evidence", task="alpha-build")])
    now[0] = 4
    assert workspace.document(path)["freshness"]["state"] == "needs_review"


def test_attention_respects_corrections_from_another_task(workspace):
    now = datetime.now(timezone.utc).isoformat()
    memory.append_events(workspace.root, [record("wait", task="alpha-build", content={
        "what": "Waiting", "where": ["host"], "agent_runtime": {
            "agent": "codex", "machine_id": "host-a", "session_id": "thread",
            "state": "waiting", "observed_at": now},
    }), record("correction", task="beta-check", supersedes=["wait"])])
    assert workspace.timeline()["attention_count"] == 0
    assert workspace.timeline("alpha", "alpha-build")["task_detail"]["execution"]["state"] == "unknown"


def test_running_session_does_not_hide_another_session_error(workspace):
    now = datetime.now(timezone.utc).isoformat()
    events = [record(state, task="alpha-build", content={
        "what": state, "where": ["host observation"], "agent_runtime": {
            "agent": "codex", "machine_id": "host-a", "session_id": state,
            "state": state, "observed_at": now},
    }) for state in ("running", "error", "waiting")]
    memory.append_events(workspace.root, events)
    attention = workspace.timeline()["attention"][0]
    assert attention["execution"]["state"] == "running"
    assert "Agent 等待输入" in attention["reasons"] and "Agent 报告错误" in attention["reasons"]


def test_corrected_explicit_mapping_does_not_inherit_old_target(workspace):
    memory.append_events(workspace.root, [
        record("old", business=["alpha"]),
        record("corrected", business=["beta"], supersedes=["old"]),
        record("continued"),
    ])
    assert workspace.events({"business": "beta"})["total"] == 2
    assert workspace.events({"business": "alpha"})["total"] == 0


@pytest.mark.parametrize("task_id", ["alpha-build", "shared"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_corrected_mapping_ignores_superseded_curated_inheritance(workspace, task_id, wrapped):
    old = record("old", task=task_id, business=["alpha"])
    home = workspace.root / ".share/knowledge/projects/alpha.json"
    home.write_text(json.dumps({"events": [{"event": old} if wrapped else old]}))
    memory.append_events(workspace.root, [old,
        record("corrected", task=task_id, business=["beta"], supersedes=["old"]),
        record("continued", task=task_id),
    ])
    assert {e["event_id"] for e in workspace.events({"business": "beta"})["items"]} == {"corrected", "continued"}
    assert workspace.events({"business": "alpha"})["total"] == 0
    assert workspace.timeline("alpha")["projects"][0]["event_count"] == 1


def test_obsolete_raw_status_does_not_become_current_curated_inheritance(workspace):
    old = record("old", business=["alpha"])
    home = workspace.root / ".share/knowledge/projects/alpha.json"
    home.write_text(json.dumps({"events": [old]}))
    memory.append_events(workspace.root, [old | {"status": "superseded"},
        record("corrected", business=["beta"]), record("continued"),
    ])
    assert {e["event_id"] for e in workspace.events({"business": "beta"})["items"]} == {"corrected", "continued"}
    assert workspace.timeline("alpha")["projects"][0]["event_count"] == 1


@pytest.mark.parametrize("task_id", ["alpha-build", "shared"])
def test_current_explicit_source_assignment_wins_over_curated_inheritance(workspace, task_id):
    source = record("source", task=task_id, business=["beta"])
    home = workspace.root / ".share/knowledge/projects/alpha.json"
    home.write_text(json.dumps({"events": [source]}))
    memory.append_events(workspace.root, [source, record("continued", task=task_id)])
    assert {e["event_id"] for e in workspace.events({"business": "beta"})["items"]} == {"source", "continued"}
    assert workspace.events({"business": "alpha"})["total"] == 0


def test_explicitly_unassigned_source_does_not_create_curated_task_inheritance(workspace):
    source = record("source", business=[])
    home = workspace.root / ".share/knowledge/projects/alpha.json"
    home.write_text(json.dumps({"events": [source]}))
    memory.append_events(workspace.root, [source, record("continued")])
    assert workspace.events({"business": "alpha"})["total"] == 0
    assert workspace.timeline()["coverage"]["unassigned_events"] == 2


def test_cross_project_task_context_is_complete_but_milestones_stay_scoped(workspace):
    memory.append_events(workspace.root, [
        record("obsolete", business=["alpha"], event_type="decision"),
        record("decision", business=["alpha"], event_type="decision", supersedes=["obsolete"]),
        record("result", business=["beta"], event_type="test_result"),
        record("risk", business=["beta"], event_type="risk"),
        record("unrelated", task="other", business=["beta"], event_type="test_result"),
    ])
    for name, milestone_ids in (("alpha", {"decision"}), ("beta", {"result", "risk"})):
        response = workspace.timeline(name, "shared")
        detail = response["task_detail"]
        assert [e["event_id"] for e in detail["decisions"]] == ["decision"]
        assert [e["event_id"] for e in detail["results"]] == ["result"]
        assert [e["event_id"] for e in detail["attention"]] == ["risk"]
        assert {e["event_id"] for e in response["projects"][0]["milestones"]} == milestone_ids


def test_new_routes_are_read_only_and_validate_scope(workspace):
    memory.append_events(workspace.root, [record("fact", task="alpha-build")])
    before = {p.relative_to(workspace.root): p.read_bytes() for p in workspace.root.rglob("*") if p.is_file()}
    server = Server(("127.0.0.1", 0), workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/handoff?task=alpha-build&to_agent=claude") as response:
            data = json.load(response)
        assert data["handoff"] == {"state": "prepared", "to_agent": "claude", "dispatched": False}
        with urlopen(base + "/api/review?task=alpha-build") as response:
            assert json.load(response)["state"] == "prepared"
        for path, code in (("/api/review", 400), ("/api/review?task=missing", 404),
                           ("/api/review?task=alpha-build&max_chars=5", 400),
                           ("/api/review?task=alpha-build&limit=0", 400),
                           ("/api/handoff?task=alpha-build&to_agent=invalid", 400)):
            with pytest.raises(HTTPError) as error:
                urlopen(base + path)
            assert error.value.code == code
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/api/review", data=b"{}"))
        assert error.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert before == {p.relative_to(workspace.root): p.read_bytes() for p in workspace.root.rglob("*") if p.is_file()}
