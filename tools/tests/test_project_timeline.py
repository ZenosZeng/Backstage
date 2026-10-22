"""Project timelines show recorded evidence, without inventing project schedules."""

import json
import threading
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from scripts import memory
from tools.web.__main__ import Server
from tools.web.data import Workspace


@pytest.fixture
def timeline_workspace(tmp_path):
    root = tmp_path / ".agents"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({
        "workspace": "timeline-test", "workspace_root": str(tmp_path), "machine_id": "machine-a",
        "projects": {name: {"path": name} for name in ("repo-a", "repo-b")},
    }))
    folder = root / ".share/knowledge/projects"
    folder.mkdir(parents=True)
    (folder / "alpha.md").write_text("# Alpha research\n\nA curated business project.\n")
    return Workspace(root, root / ".local")


def event(key, at="2026-10-01T00:00:00Z", *, task="alpha-build", repo="repo-a", **changes):
    return {
        "schema_version": 1, "event_id": key, "created_at": at,
        "machine_id": "machine-a", "agent": "codex", "workspace": "timeline-test",
        "projects": [{"name": repo}], "event_type": "progress", "topic_key": "work/update",
        "title": key, "content": {"what": "Short human-readable progress", "how": "FULL-RAW-CONTENT"},
        "status": "active", "task_id": task, **changes,
    }


def lifecycle(key, state, at, *, supersedes=(), **changes):
    return event(key, at, event_type="decision", supersedes=list(supersedes),
                 content={"what": f"Explicit {state}", "where": ["user request"], "task_state": state},
                 **changes)


def curated(workspace, name, events):
    home = workspace.root / ".share/knowledge/projects" / f"{name}.md"
    if not home.exists():
        home.write_text(f"# {name.title()}\n\nAnother business project.\n")
    home.with_suffix(".json").write_text(json.dumps({"events": events}))


@pytest.fixture
def timeline_server(timeline_workspace):
    httpd = Server(("127.0.0.1", 0), timeline_workspace)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def fetch(base, path):
    with urlopen(base + path) as response:
        return json.load(response)


def test_summary_uses_business_membership_across_repositories(timeline_workspace):
    workspace = timeline_workspace
    first = event("first", task="cross-repo")
    continued = event("continued", "2026-10-03T00:00:00Z", task="cross-repo", repo="repo-b")
    unrelated = event("unrelated-same-repository", task="unrelated")
    memory.append_events(workspace.root, [first, continued, unrelated])
    curated(workspace, "alpha", [first])
    response = workspace.timeline()
    assert response["project_mode"] == "knowledge"
    assert len(response["projects"]) == 1
    project = response["projects"][0]
    assert project["name"] == "alpha" and project["title"] == "Alpha research"
    assert project["repositories"] == ["repo-a", "repo-b"]
    assert project["event_count"] == 2 and project["task_count"] == 1
    assert project["task_counts"] == {"active": 0, "paused": 0, "closed": 0, "unknown": 1, "conflict": 0}
    assert "tasks" not in project and "milestones" not in project
    assert "content" not in json.dumps(response) and "FULL-RAW-CONTENT" not in json.dumps(response)
    assert response["range"] == {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-03T00:00:00+00:00"}


def test_closed_task_keeps_status_after_ordinary_progress_and_lifecycle_history(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [
        event("human-task-title", "2026-10-01T00:00:00Z"),
        lifecycle("start", "active", "2026-10-02T00:00:00Z"),
        lifecycle("close", "closed", "2026-10-03T00:00:00Z", supersedes=["start"]),
        event("post-close-evidence", "2026-10-04T00:00:00Z"),
    ])
    project = workspace.timeline("alpha")["projects"][0]
    task = project["tasks"][0]
    assert task["state"] == "closed"
    assert task["title"] == "human-task-title" and task["last_title"] == "post-close-evidence"
    assert task["last_recorded_at"] == "2026-10-04T00:00:00+00:00"
    assert task["state_heads"] == [{"event_id": "close", "created_at": "2026-10-03T00:00:00+00:00", "task_state": "closed"}]
    milestones = {e["event_id"]: e for e in project["milestones"]}
    assert milestones["start"]["is_historical"] and milestones["start"]["task_state"] == "active"
    assert not milestones["close"]["is_historical"]
    assert "state" not in project and "completed_at" not in project and "started_at" not in project
    assert all("content" not in e for e in project["milestones"])


def test_explicit_resume_reopens_task(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [
        lifecycle("start", "active", "2026-10-01T00:00:00Z"),
        lifecycle("pause", "paused", "2026-10-02T00:00:00Z", supersedes=["start"]),
        lifecycle("resume", "active", "2026-10-03T00:00:00Z", supersedes=["pause"]),
    ])
    project = workspace.timeline("alpha")["projects"][0]
    assert project["tasks"][0]["state"] == "active"
    assert project["tasks"][0]["title"] == "alpha-build"
    assert [e["task_state"] for e in project["milestones"]] == ["active", "paused", "active"]


def test_parallel_lifecycle_heads_remain_conflict(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [
        lifecycle("start", "active", "2026-10-01T00:00:00Z"),
        lifecycle("pause", "paused", "2026-10-02T00:00:00Z", supersedes=["start"]),
        lifecycle("close", "closed", "2026-10-03T00:00:00Z", supersedes=["start"], machine_id="machine-b"),
    ])
    project = workspace.timeline("alpha")["projects"][0]
    assert project["task_counts"]["conflict"] == 1
    assert project["tasks"][0]["state"] == "conflict"
    assert {e["event_id"] for e in project["tasks"][0]["state_heads"]} == {"pause", "close"}


def test_task_state_is_global_even_when_lifecycle_is_outside_project_membership(timeline_workspace):
    workspace = timeline_workspace
    first = event("alpha-evidence", task="shared-day")
    second = event("beta-evidence", task="shared-day")
    memory.append_events(workspace.root, [first, second, lifecycle(
        "global-close", "closed", "2026-10-04T00:00:00Z", task="shared-day",
    )])
    curated(workspace, "alpha", [first])
    curated(workspace, "beta", [second])
    project = workspace.timeline("alpha")["projects"][0]
    assert project["event_count"] == 1
    assert project["tasks"][0]["state"] == "closed"
    assert project["tasks"][0]["state_heads"][0]["event_id"] == "global-close"
    assert {e["event_id"] for e in project["milestones"]} == {"alpha-evidence"}


def test_empty_project_has_no_invented_dates_or_state(timeline_workspace):
    response = timeline_workspace.timeline("alpha")
    project = response["projects"][0]
    assert project["first_recorded_at"] is None and project["last_recorded_at"] is None
    assert project["tasks"] == [] and project["milestones"] == []
    assert project["task_count"] == 0 and project["milestone_total"] == 0
    assert response["range"] == {"start": None, "end": None}


@pytest.mark.parametrize("task_id", [None, "-"])
def test_project_record_without_task_does_not_create_task(timeline_workspace, task_id):
    workspace = timeline_workspace
    record = event("project-decision", task=task_id, event_type="decision")
    memory.append_events(workspace.root, [record])
    curated(workspace, "alpha", [record])
    project = workspace.timeline("alpha")["projects"][0]
    assert project["task_count"] == 0 and project["tasks"] == []
    assert len(project["milestones"]) == 1 and project["milestones"][0]["task_id"] == task_id


def test_utc_ordering_and_single_point_ranges(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [
        event("earlier", "2026-10-02T00:30:00+02:00"),
        event("later", "2026-10-01T23:00:00Z"),
        event("single", "2026-10-02T03:00:00+02:00", task="alpha-single"),
    ])
    response = workspace.timeline("alpha")
    project = response["projects"][0]
    assert response["range"] == {"start": "2026-10-01T22:30:00+00:00", "end": "2026-10-02T01:00:00+00:00"}
    assert [e["event_id"] for e in project["milestones"]] == ["earlier", "single"]
    single = next(t for t in project["tasks"] if t["task_id"] == "alpha-single")
    assert single["first_recorded_at"] == single["last_recorded_at"] == "2026-10-02T01:00:00+00:00"


def test_project_sorting_uses_utc_instead_of_timestamp_spelling(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [
        event("alpha", "2026-10-02T00:30:00+02:00"),
        event("beta", "2026-10-01T23:00:00Z", task="beta-build"),
    ])
    curated(workspace, "beta", [])
    response = workspace.timeline()
    assert [p["name"] for p in response["projects"]] == ["beta", "alpha"]
    assert response["range"] == {"start": "2026-10-01T22:30:00+00:00", "end": "2026-10-01T23:00:00+00:00"}


def test_task_filter_keeps_full_task_rows_and_filters_milestones(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [event("a"), event("b", task="alpha-other")])
    project = workspace.timeline("alpha", "alpha-build")["projects"][0]
    assert {t["task_id"] for t in project["tasks"]} == {"alpha-build", "alpha-other"}
    assert [e["event_id"] for e in project["milestones"]] == ["a"]
    with pytest.raises(FileNotFoundError):
        workspace.timeline("alpha", "unrelated-task")
    with pytest.raises(ValueError):
        workspace.timeline(task="alpha-build")


def test_milestone_limit_preserves_history_ends_and_reports_omission(timeline_workspace):
    workspace = timeline_workspace
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    records = [event(f"decision-{i:03}", (start + timedelta(hours=i)).isoformat(), event_type="decision")
               for i in range(101)]
    memory.append_events(workspace.root, records)
    project = workspace.timeline("alpha")["projects"][0]
    milestones = project["milestones"]
    assert len(milestones) == len({e["event_id"] for e in milestones}) == 40
    assert milestones[0]["event_id"] == "decision-000" and milestones[-1]["event_id"] == "decision-100"
    assert project["milestone_total"] == 101 and project["milestone_omitted"] == 61
    assert [e["created_at"] for e in milestones] == sorted(e["created_at"] for e in milestones)


def test_obsolete_ordinary_records_are_not_key_milestones(timeline_workspace):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [
        event("outdated", event_type="decision"),
        event("correction", "2026-10-02T00:00:00Z", event_type="bugfix", supersedes=["outdated"]),
    ])
    project = workspace.timeline("alpha")["projects"][0]
    assert project["first_recorded_at"] == "2026-10-01T00:00:00+00:00"
    assert [e["event_id"] for e in project["milestones"]] == ["correction"]


def test_repository_fallback_is_explicit_and_unknown_project_is_rejected(timeline_workspace):
    workspace = timeline_workspace
    (workspace.root / ".share/knowledge/projects/alpha.md").unlink()
    memory.append_events(workspace.root, [event("unorganized")])
    response = workspace.timeline()
    assert response["project_mode"] == "repositories"
    assert {p["kind"] for p in response["projects"]} == {"repository"}
    project = workspace.timeline("repo-a")["projects"][0]
    assert project["home"] is None and project["repositories"] == ["repo-a"]
    assert project["tasks"][0]["task_id"] == "alpha-build"
    with pytest.raises(FileNotFoundError):
        workspace.timeline("alpha")


def test_timeline_route_is_read_only_and_reports_invalid_selections(timeline_workspace, timeline_server):
    workspace = timeline_workspace
    memory.append_events(workspace.root, [event("a")])
    before = {p.relative_to(workspace.root): p.read_bytes() for p in workspace.root.rglob("*") if p.is_file()}
    assert "tasks" not in fetch(timeline_server, "/api/timeline")["projects"][0]
    detail = fetch(timeline_server, "/api/timeline?project=alpha&task=alpha-build")
    assert detail["projects"][0]["milestones"][0]["event_id"] == "a"
    assert "content" not in json.dumps(detail) and "FULL-RAW-CONTENT" not in json.dumps(detail)
    for path, code in (("/api/timeline?project=missing", 404),
                       ("/api/timeline?project=alpha&task=missing", 404),
                       ("/api/timeline?task=alpha-build", 400)):
        with pytest.raises(HTTPError) as exc:
            fetch(timeline_server, path)
        assert exc.value.code == code
    assert before == {p.relative_to(workspace.root): p.read_bytes() for p in workspace.root.rglob("*") if p.is_file()}


def test_timeline_reuses_snapshot_and_keeps_read_errors_visible(timeline_workspace, monkeypatch):
    from unittest.mock import Mock

    workspace = timeline_workspace
    memory.append_events(workspace.root, [event("a")])
    broken = workspace.root / ".share/memory/broken/codex/2026-10-01.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("invalid JSON")
    spy = Mock(wraps=memory.load_events)
    monkeypatch.setattr(memory, "load_events", spy)
    overview = workspace.overview()
    assert workspace.timeline()["sampled_at"] == overview["sampled_at"]
    assert workspace.timeline("alpha")["errors"]
    assert spy.call_count == 1
