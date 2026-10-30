"""Exercise read-only routes against an isolated workspace, never real services."""

import json
import subprocess
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts import doctor, memory
from tools.web.__main__ import Handler, Server
from tools.web.data import Workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from tools.watchdog import loader
    monkeypatch.setattr(loader, "available_adapters", lambda: ["demo", "unused"])
    root = tmp_path / ".agents"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({
        "workspace": "test-workspace", "workspace_root": str(tmp_path), "machine_id": "machine-a",
        "projects": {"example": {"path": "example"}},
    }))
    def event(event_id, what, **changes):
        return {
            "schema_version": 1, "event_id": event_id, "created_at": "2026-10-15T01:00:00Z",
            "machine_id": "machine-a", "agent": "codex", "workspace": "test-workspace",
            "projects": [{"name": "example"}], "event_type": "fact", "topic_key": "web/test",
            "title": event_id, "content": {"what": what}, "status": "active", "task_id": "example-task",
            **changes,
        }
    memory.append_events(root, [
        event("old", "old finding"),
        event("new", "confirmed finding", supersedes=["old"]),
        event("active", "explicit active task", event_type="decision",
              content={"what": "started", "where": ["user"], "task_state": "active"}),
    ])
    docs = root / ".share/knowledge"
    docs.mkdir(parents=True)
    (docs / "start.md").write_text('# Start\n\n[Next](next.md)\n\n![Diagram](chart.svg)\n\n<script>window.bad=1</script>\n\n[Bad](javascript:alert(1))\n\n| A | B |\n|---|---|\n| 1 | 2 |\n')
    (docs / "next.md").write_text("# Next\n\n## Details\n\nProject notes")
    (docs / "chart.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text y="20">Test</text></svg>')
    (root / "README.md").write_text("# Backstage test\n")
    return Workspace(root, root / ".local")


@pytest.fixture
def server(workspace):
    httpd = Server(("127.0.0.1", 0), workspace)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def fetch(base, path):
    with urlopen(base + path) as response:
        return json.load(response)


def test_overview_reuses_explicit_task_states_and_source_metadata(workspace):
    data = workspace.overview()
    assert data["workspace"] == "test-workspace"
    assert data["counts"]["active_tasks"] == 1
    assert data["projects"][0]["name"] == "example"
    assert data["counts"]["documents"] == 4
    assert data["machines"] == [{"name": "machine-a", "last_event_at": "2026-10-15T01:00:00Z"}]
    assert data["recent"][0]["source"].startswith(".share/memory/")


def test_event_filter_detail_and_superseded_history(server):
    data = fetch(server, "/api/events?project=example&q=finding")
    assert [e["event_id"] for e in data["items"]] == ["new"]
    history = fetch(server, "/api/events?current=false&q=finding&limit=1")
    assert history["total"] == 2 and len(history["items"]) == 1
    detail = fetch(server, "/api/event?id=old")
    assert detail["replaced"] and detail["replaced_by"] == ["new"]
    assert fetch(server, "/api/events?machine=other")["total"] == 0
    assert fetch(server, "/api/events?type=decision")["total"] == 1


def test_document_preview_escapes_html_and_rewrites_relative_links(server):
    data = fetch(server, "/api/document?path=.share/knowledge/start.md")
    assert "<script>" not in data["html"]
    assert "&lt;script&gt;" in data["html"]
    assert '<a href="javascript:' not in data["html"]
    assert '#/documents?path=.share%2Fknowledge%2Fnext.md' in data["html"]
    assert 'api/asset?path=.share/knowledge/chart.svg' in data["html"]
    assert "<table>" in data["html"]


def test_public_tool_and_onboarding_docs_open_from_readme(workspace, server):
    for relative in ("tools/web/README.md", "examples/README.md"):
        path = workspace.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Public documentation\n")
        assert "Public documentation" in fetch(server, "/api/document?path=" + relative)["html"]
    assert len(workspace.documents()) == 6


@pytest.mark.parametrize("path", ["../config.json", "config.json", ".share/memory/machine-a/codex/2026-10-15.json", "tools/web/data.py"])
def test_document_routes_deny_private_and_outside_paths(server, path):
    with pytest.raises(HTTPError) as exc:
        urlopen(server + "/api/document?path=" + path)
    assert exc.value.code == 403


def test_document_symlink_cannot_escape_allowlisted_roots(workspace, server, tmp_path):
    secret = tmp_path / "private.md"
    secret.write_text("private")
    (workspace.root / ".share/knowledge/escape.md").symlink_to(secret)
    assert not any(d["path"].endswith("escape.md") for d in workspace.documents())
    with pytest.raises(HTTPError) as exc:
        urlopen(server + "/api/asset?path=.share/knowledge/escape.md")
    assert exc.value.code == 403


def test_svg_is_sandboxed_and_static_assets_are_served(server):
    with urlopen(server + "/api/asset?path=.share/knowledge/chart.svg") as response:
        assert "sandbox" in response.headers["Content-Security-Policy"]
        assert response.headers["Content-Type"] == "image/svg+xml"
    for path in ("/", "/app.js", "/app.css", "/theme.js", "/cluster.js", "/cluster.css", "/api.js"):
        with urlopen(server + path) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(HTTPError) as exc:
        urlopen(server + "/config.json")
    assert exc.value.code == 404


def test_no_write_control_or_rebinding_routes(server):
    with pytest.raises(HTTPError) as exc:
        urlopen(Request(server + "/api/restart", data=b"{}", method="POST"))
    assert exc.value.code == 405
    with pytest.raises(HTTPError) as exc:
        urlopen(Request(server + "/api/overview", headers={"Host": "attacker.example"}))
    assert exc.value.code == 403


def test_monitor_uses_existing_once_without_notifications(workspace, monkeypatch):
    config = workspace.local / "config/watchdog-demo.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"notify":true}')
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"status":"step 10/100, loss 0.2"}')
    monkeypatch.setattr(subprocess, "run", run)
    before = config.read_bytes()
    assert "step 10" in workspace.monitor("demo")["status"]
    assert "--once" in calls[0][0] and "--no-notify" in calls[0][0]
    assert calls[0][1]["env"]["AGENTS_LOCAL_ROOT"] == str(workspace.local)
    assert config.read_bytes() == before
    assert workspace.monitor("unused")["configured"] is False


def test_monitor_list_discovers_local_adapters_and_orphaned_configs(workspace, server, monkeypatch):
    config = workspace.local / "config/watchdog-missing.json"
    config.parent.mkdir(parents=True)
    config.write_text('{}')
    (config.parent / "watchdog-invalid!.json").write_text('{}')
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("listing must not collect"))
    items = {item["name"]: item for item in fetch(server, "/api/monitors")["items"]}
    assert set(items) == {"demo", "unused", "missing"}
    assert items["demo"] == {"name": "demo", "adapter": "demo", "available": True, "configured": False}
    assert items["missing"] == {"name": "missing", "adapter": "missing", "available": False, "configured": True}
    assert fetch(server, "/api/monitor?kind=missing")["state"] == "unavailable"


def test_monitor_instance_can_select_another_local_adapter(workspace, monkeypatch):
    config = workspace.local / "config/watchdog-nightly.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"adapter":"demo"}')
    item = next(item for item in workspace.monitors()["items"] if item["name"] == "nightly")
    assert item["adapter"] == "demo" and item["available"] and item["configured"]
    commands = []
    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '{"status":"ready"}')
    monkeypatch.setattr(subprocess, "run", run)
    assert workspace.monitor("nightly")["status"] == "ready"
    assert commands[0][3] == "nightly"


@pytest.mark.parametrize("name", ["unknown", "../secret", "demo/other", "demo!"])
def test_monitor_rejects_uninstalled_or_invalid_names(workspace, name):
    with pytest.raises(ValueError):
        workspace.monitor(name)


@pytest.mark.parametrize("payload", ["not JSON", "{}", "[]", '{"status":null}', '{"status":[]}'])
def test_invalid_collector_payload_is_unavailable(workspace, monkeypatch, payload):
    config = workspace.local / "config/watchdog-demo.json"
    config.parent.mkdir(parents=True)
    config.write_text('{}')
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, payload))
    assert workspace.monitor("demo")["state"] == "unavailable"


def cluster_fixture(workspace, *, at=1000, state="collecting"):
    from tools.cluster.config import settings_fingerprint
    from tools.config import atomic_json

    config = {"hosts": [{"alias": "gpu-test", "label": "测试节点", "group": "GPU"}]}
    atomic_json(workspace.local / "config/cluster-monitor.json", config)
    snapshot = {
        "schema_version": 1, "state": state, "config_hash": settings_fingerprint(config),
        "time": at, "interval": 5, "health_interval": 30,
        "nodes": [{"alias": "gpu-test", "label": "测试节点", "group": "GPU", "status": "online",
                   "received_at": at, "sample": {"sampled_at": at, "cpu_percent": 25}, "history": [], "error": None}],
        "summary": {"nodes": 1, "online": 1, "gpus": 0}, "ports": None,
    }
    path = workspace.local / "tools/cluster-monitor/snapshot.json"
    atomic_json(path, snapshot)
    return config, path, snapshot


def test_cluster_http_only_reads_selected_local_files(server, workspace, monkeypatch):
    from tools import service
    from tools.web import data

    _, path, _ = cluster_fixture(workspace)
    monkeypatch.setattr(data.time, "time", lambda: 1001)
    monkeypatch.setattr(service, "running_record", lambda *_: {"pid": 123})
    def forbidden(*_args, **_kwargs):
        raise AssertionError("HTTP must not start SSH, probes, or a service")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = {p: p.read_bytes() for p in workspace.local.rglob("*") if p.is_file()}
    result = fetch(server, "/api/cluster")
    assert result["configured"] and result["collector_running"]
    assert result["state"] == "available" and result["scope"] == "cluster"
    assert result["source"] == str(path)
    assert result["sampled_at"] != result["read_at"]
    assert result["snapshot"]["nodes"][0]["label"] == "测试节点"
    assert before == {p: p.read_bytes() for p in workspace.local.rglob("*") if p.is_file()}


@pytest.mark.parametrize("running,age,state,expected", [
    (True, 1, "collecting", "available"),
    (False, 1, "collecting", "stale"),
    (True, 21, "collecting", "stale"),
    (True, -10, "collecting", "stale"),
    (True, 1, "stopped", "stale"),
])
def test_cluster_never_reports_stopped_or_expired_snapshot_as_live(workspace, monkeypatch, running, age, state, expected):
    from tools import service
    from tools.web import data

    _, path, _ = cluster_fixture(workspace, state=state)
    before = path.read_bytes()
    monkeypatch.setattr(data.time, "time", lambda: 1000 + age)
    monkeypatch.setattr(service, "running_record", lambda *_: {"pid": 123} if running else None)
    result = workspace.cluster()
    assert result["state"] == expected
    assert result["collector_running"] is running
    assert result["snapshot"]["nodes"][0]["status"] == ("online" if expected == "available" else "stale")
    assert result["snapshot"]["nodes"][0]["sample"]["cpu_percent"] == 25
    assert path.read_bytes() == before  # ageing a read never rewrites the collector's file


def test_cluster_missing_invalid_and_changed_config_are_distinct(workspace, monkeypatch):
    from tools import service
    from tools.config import atomic_json

    assert workspace.cluster()["state"] == "unconfigured"
    monkeypatch.setattr(service, "running_record", lambda *_: None)
    config, path, snapshot = cluster_fixture(workspace)
    path.unlink()
    assert workspace.cluster()["state"] == "unavailable"
    monkeypatch.setattr(service, "running_record", lambda *_: {"pid": 123})
    assert workspace.cluster()["state"] == "no_data"
    path.write_text("not JSON")
    assert workspace.cluster()["state"] == "unavailable"
    atomic_json(path, snapshot)
    config["hosts"][0]["alias"] = "another-node"
    atomic_json(workspace.local / "config/cluster-monitor.json", config)
    result = workspace.cluster()
    assert result["state"] == "no_data" and result["snapshot"] is None
    assert "需重启" in result["status"]
    atomic_json(workspace.local / "config/cluster-monitor.json", {"hosts": []})
    assert workspace.cluster()["state"] == "unavailable"


def test_cluster_does_not_read_another_local_root(workspace, tmp_path, monkeypatch):
    from tools import service
    from tools.web import data

    _, path, _ = cluster_fixture(workspace)
    alternate = Workspace(workspace.root, tmp_path / "alternate-local")
    assert alternate.cluster()["state"] == "unconfigured"
    config, _, snapshot = cluster_fixture(alternate)
    snapshot["nodes"][0]["sample"]["cpu_percent"] = 73
    from tools.config import atomic_json
    atomic_json(alternate.local / "tools/cluster-monitor/snapshot.json", snapshot)
    monkeypatch.setattr(data.time, "time", lambda: 1001)
    monkeypatch.setattr(service, "running_record", lambda *_: {"pid": 123})
    assert alternate.cluster()["snapshot"]["nodes"][0]["sample"]["cpu_percent"] == 73
    assert json.loads(path.read_text())["nodes"][0]["sample"]["cpu_percent"] == 25


def test_cluster_api_preserves_host_and_read_only_boundaries(server):
    with pytest.raises(HTTPError) as exc:
        urlopen(Request(server + "/api/cluster", headers={"Host": "attacker.example"}))
    assert exc.value.code == 403
    with pytest.raises(HTTPError) as exc:
        urlopen(Request(server + "/api/cluster", data=b"{}", method="POST"))
    assert exc.value.code == 405


def test_collection_error_is_unavailable_not_empty(workspace, monkeypatch):
    config = workspace.local / "config/watchdog-demo.json"
    config.parent.mkdir(parents=True)
    config.write_text('{}')
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(
        command, 1, "", "private collection error"))
    data = workspace.monitor("demo")
    assert data["state"] == "unavailable"
    assert "private collection" not in json.dumps(data)
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(
        command, 0, '{"status":""}'))
    assert workspace.monitor("demo")["state"] == "no_data"


def test_data_errors_are_visible_instead_of_healthy_empty_state(workspace):
    path = workspace.root / ".share/memory/broken/codex/2026-10-15.json"
    path.parent.mkdir(parents=True)
    path.write_text("invalid JSON")
    data = workspace.overview()
    assert data["errors"] and data["counts"]["events"] == 3


def test_context_route_is_bounded_and_keeps_source_semantics(server):
    data = fetch(server, "/api/context?id=old&before=0&after=0")
    assert {e["event_id"] for e in data["items"]} == {"old", "new"}
    assert next(e for e in data["items"] if e["event_id"]=="old")["replaced"]
    assert data["errors"] == []
    with pytest.raises(HTTPError) as exc:
        fetch(server, "/api/context?id=missing")
    assert exc.value.code == 404
    with pytest.raises(HTTPError) as exc:
        fetch(server, "/api/context?id=old&before=100")
    assert exc.value.code == 400


def test_doctor_health_lists_actionable_events_without_modification(workspace):
    before = {p: p.read_bytes() for p in workspace.root.rglob("*.json")}
    data = workspace.doctor()
    health = data["memory_health"]
    assert health["checked_events"] == 3
    assert any(i["event_id"]=="new" and i["source"] for i in health["items"])
    assert len(health["items"]) <= 50
    assert {p: p.read_bytes() for p in workspace.root.rglob("*.json")} == before


def test_services_compare_start_metadata_and_preserve_unknown_old_records(workspace, monkeypatch):
    from scripts import doctor
    from tools import service
    monkeypatch.setattr(doctor, "code_identity", lambda root: {"version": "0.8.0", "commit": "new", "dirty": False})
    def record(directory, name):
        if name=="claude-feishu":
            return {"pid": 123, "ready": True, "code_at_start": {"version": "0.7.1", "commit": "old", "dirty": False}}
        if name=="watchdog-demo":
            return {"pid": 124, "ready": True}  # 部署前的旧记录
        return None
    monkeypatch.setattr(service, "running_record", record)
    monkeypatch.setattr(service, "service_names", lambda root=None: ("claude-feishu", "watchdog-demo"))
    data = workspace.services()
    items = {item["name"]: item for item in data["items"]}
    assert items["claude-feishu"]["code_changed"] is True
    assert items["watchdog-demo"]["code_at_start"] is None
    assert items["watchdog-demo"]["code_changed"] is None


def test_disconnected_client_does_not_trigger_a_second_error_response(workspace):
    class ClosedStream:
        def write(self, _data):
            raise BrokenPipeError("browser navigated away")

    handler = object.__new__(Handler)
    handler.server = SimpleNamespace(server_port=8787, workspace=workspace)
    handler.headers = {"Host": "127.0.0.1:8787"}
    handler.path = "/api/overview"
    responses = []
    handler.send_response = responses.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.wfile = ClosedStream()
    handler.do_GET()
    assert responses == [200]


@pytest.fixture
def business_workspace(workspace):
    existing, _ = memory.load_events(workspace.root)
    base = next(e for e in existing if e['event_id'] == 'new')
    def fact(key, task, repo):
        return base | {'event_id': key, 'task_id': task, 'projects': [{'name': repo}], 'supersedes': []}
    facts = [fact('seed-a', 'cross-run', 'repo-a'), fact('seed-b', 'shared-day', 'repo-b'),
             fact('seed-other', 'shared-day', 'repo-a'), fact('continued', 'cross-run', 'repo-c'),
             fact('same-repo-unrelated', 'separate-work', 'repo-a'),
             fact('ambiguous-future', 'shared-day', 'repo-a'), fact('new-namespace', 'research-next', 'repo-c')]
    memory.append_events(workspace.root, facts + [fact('research-active', 'cross-run', 'repo-c') | {
        'event_type': 'decision', 'content': {'what': 'start', 'where': ['user'], 'task_state': 'active'},
    }])
    folder = workspace.root / '.share/knowledge/projects'
    folder.mkdir()
    (folder / 'research.md').write_text('# Cross-repository research\n\nA single business project.\n\n[Report](../../docs/shared/report.md)\n')
    (folder / 'other.md').write_text('# Other business\n\nSeparate work.\n')
    (folder / 'research.json').write_text(json.dumps({'events': facts[:2]}))
    (folder / 'other.json').write_text(json.dumps({'events': [{'event': facts[2], 'source': 'original daily file'}]}))
    report = workspace.root / '.share/docs/shared/report.md'
    report.parent.mkdir(parents=True)
    report.write_text('# Experiment report\n')
    return workspace


def test_business_projects_use_curated_events_and_unambiguous_tasks(business_workspace):
    w = business_workspace
    overview = w.overview()
    assert overview['counts']['projects'] == 2
    assert {p['name'] for p in overview['projects']} == {'research', 'other'}
    assert len(overview['repositories']) > 2
    project = next(p for p in overview['projects'] if p['name'] == 'research')
    assert project['title'] == 'Cross-repository research'
    assert project['active_tasks'] == 1
    assert project['repositories'] == ['repo-a', 'repo-b', 'repo-c']
    selected = w.events({'business': 'research'})['items']
    assert {e['event_id'] for e in selected} == {'seed-a', 'seed-b', 'continued', 'new-namespace', 'research-active'}
    assert {e['event_id'] for e in w.events({'business': 'other'})['items']} == {'seed-other'}
    narrowed = w.events({'business': 'research', 'project': 'repo-c'})['items']
    assert {e['event_id'] for e in narrowed} == {'continued', 'new-namespace', 'research-active'}
    assert all(e['source'].startswith('.share/memory/') for e in narrowed)
    assert w.event('continued')['event']['projects'] == [{'name': 'repo-c'}]


def test_business_documents_follow_knowledge_links_not_filename_search(business_workspace):
    docs = business_workspace.documents('research')
    assert {d['path'] for d in docs} == {
        '.share/knowledge/projects/research.md', '.share/docs/shared/report.md',
    }
    with pytest.raises(FileNotFoundError):
        business_workspace.documents('nonexistent')


def test_one_snapshot_is_shared_by_read_routes_until_ttl_then_refreshes(workspace, monkeypatch):
    from unittest.mock import Mock
    now = [0.0]
    w = Workspace(workspace.root, workspace.local, clock=lambda: now[0])
    spy = Mock(wraps=memory.load_events)
    monkeypatch.setattr(memory, 'load_events', spy)
    original = w.overview()
    w.events({})
    w.event('new')
    w.documents()
    assert spy.call_count == 1
    old, _ = memory.load_events(workspace.root)
    added = old[0] | {'event_id': 'appended', 'supersedes': [], 'event_type': 'fact',
                     'content': {'what': 'new evidence'}}
    memory.append_events(workspace.root, [added])
    doc = workspace.root / '.share/knowledge/new.md'
    doc.write_text('# Newly added document\n')
    project = workspace.root / '.share/knowledge/projects/new-project.md'
    project.parent.mkdir()
    project.write_text('# Newly organized business\n')
    project.with_suffix('.json').write_text(json.dumps({'events': [added]}))
    spy.reset_mock()
    now[0] = 2.9
    assert w.overview() is original
    assert w.overview()['counts']['projects'] == 0
    assert w.events({'current': 'false'})['total'] == 3
    assert not any(d['path'].endswith('new.md') for d in w.documents())
    assert spy.call_count == 0
    now[0] = 3.01
    assert w.overview()['counts']['events'] == 4
    assert w.overview()['counts']['projects'] == 1
    assert 'appended' in {e['event_id'] for e in w.events({'business': 'new-project'})['items']}
    assert w.events({'current': 'false'})['total'] == 4
    assert any(d['path'].endswith('new.md') for d in w.documents())
    assert spy.call_count == 1


def test_concurrent_expired_snapshot_is_rebuilt_only_once(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock
    import time
    now = [0.0]
    w = Workspace(workspace.root, workspace.local, clock=lambda: now[0])
    original = memory.load_events
    def slow_read(*args, **kwargs):
        time.sleep(0.03)
        return original(*args, **kwargs)
    spy = Mock(side_effect=slow_read)
    monkeypatch.setattr(memory, 'load_events', spy)
    w.overview()
    now[0] = 4
    barrier = threading.Barrier(12)
    def read(_):
        barrier.wait(timeout=5)
        return w.overview()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(read, range(12)))
    assert spy.call_count == 2  # initial generation + one expiry rebuild
    assert all(result is results[0] for result in results)


@pytest.mark.parametrize('outcome,state', [('empty', 'no_data'), ('failed', 'unavailable'), ('timeout', 'unavailable')])
def test_monitor_scope_source_sampling_and_unavailable_are_explicit(workspace, monkeypatch, outcome, state):
    config = workspace.local / 'config/watchdog-demo.json'
    config.parent.mkdir(parents=True)
    config.write_text('{}')
    def run(command, **_):
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(command, 45)
        return subprocess.CompletedProcess(command, 1 if outcome == 'failed' else 0, '{"status":""}')
    monkeypatch.setattr(subprocess, 'run', run)
    data = workspace.monitor('demo')
    assert data['state'] == state and data['scope'] == 'local'
    assert data['source'] == str(config)
    assert data['sampled_at'] and '--no-notify' in data['collector']
    assert workspace.monitor('unused')['state'] == 'unconfigured'


def test_search_matches_cli_ranking_and_explicit_time_sort(workspace):
    events, _ = memory.load_events(workspace.root)
    original = events[0]
    memory.append_events(workspace.root, [original | {
        "event_id":"best-match", "title":"shared context", "content":{"what":"durable design"},
        "created_at":"2026-10-14T00:00:00Z"}, original | {
        "event_id":"incidental", "title":"latest unrelated update", "content":{"what":"mentions shared context"},
        "created_at":"2026-10-16T00:00:00Z"}])
    args = memory.parser().parse_args(['search','shared context','--current'])
    current, _ = memory.load_events(workspace.root)
    assert [e['event_id'] for e in memory.select_events(current, args)] == [e['event_id'] for e in workspace.events({'q':'shared context'})['items']]
    assert workspace.events({'q':'shared context'})['items'][0]['event_id'] == 'best-match'
    assert workspace.events({'q':'shared context','sort':'recent'})['items'][0]['event_id'] == 'incidental'
    with pytest.raises(ValueError):
        workspace.events({'sort':'invalid'})


def test_document_body_query_keeps_business_scope_and_does_not_leak_text(workspace):
    paths = workspace.root / '.share/knowledge/projects'
    paths.mkdir()
    (paths / 'example.md').write_text('# Example\n\n[Included](../next.md)')
    result = workspace.documents('example', 'Project notes')
    assert [d['path'] for d in result] == ['.share/knowledge/next.md']
    assert 'Project notes' in result[0]['excerpt']
    assert 'text' not in result[0]
    assert not workspace.documents('example', 'Backstage test')


def test_handoff_and_doctor_routes_remain_read_only_and_show_unknowns(server, workspace):
    before = {str(p):p.read_bytes() for p in workspace.root.rglob('*') if p.is_file()}
    handoff = fetch(server, '/api/handoff?task=example-task')
    assert handoff['state'] == 'active'
    assert handoff['sections']['事实与依据']
    assert handoff['repositories'][0]['commit'] is None
    assert fetch(server, '/api/doctor')['scope'] == 'local'
    assert before == {str(p):p.read_bytes() for p in workspace.root.rglob('*') if p.is_file()}
    for path in ('/api/handoff?task=missing', '/api/handoff'):
        with pytest.raises(HTTPError) as exc:
            urlopen(server + path)
        assert exc.value.code in {400,404}


def test_handoff_missing_git_is_unavailable_instead_of_missing_task(server, workspace, monkeypatch):
    (workspace.root.parent / 'example').mkdir()
    def missing_git(*args, **kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(subprocess, 'run', missing_git)
    result = fetch(server, '/api/handoff?task=example-task')
    assert result['state'] == 'active'
    repo = result['repositories'][0]
    assert repo['available'] is False
    assert repo['commit'] is repo['branch'] is repo['dirty'] is None


def test_doctor_http_uses_selected_local_root_for_conflict_and_baseline(server, workspace, tmp_path, monkeypatch):
    config_path = workspace.root / 'config.json'
    config = json.loads(config_path.read_text())
    config['sync'] = {'enabled':True, 'initialized':True, 'remote':'alias/bucket/workspace'}
    config_path.write_text(json.dumps(config))
    (workspace.root / '.local/.sync/base').mkdir(parents=True)
    workspace.local = tmp_path / 'alternate-local'
    conflict = workspace.local / '.sync/conflict'
    conflict.mkdir(parents=True)
    monkeypatch.setattr(doctor.shutil, 'which', lambda _: '/synthetic/mc')
    result = fetch(server, '/api/doctor')
    assert result['local_root'] == str(workspace.local)
    check = next(c for c in result['checks'] if c['name'] == '同步')
    assert check['state'] == 'warning'
    assert '冲突' in check['detail'] and '缺少 shared 基线' in check['detail']
    conflict.rmdir()
    (workspace.local / '.sync/base').mkdir()
    (workspace.root / '.local/.sync/conflict').mkdir()
    result = fetch(server, '/api/doctor')
    assert next(c for c in result['checks'] if c['name'] == '同步')['state'] == 'ok'


def test_document_http_unicode_excerpt_keeps_query_and_hides_full_text(server, workspace):
    path = workspace.root / 'docs'
    path.mkdir()
    (path / 'unicode.md').write_text('# Unicode\n\n' + 'ﬁ' * 100 + ' target evidence')
    result = fetch(server, '/api/documents?q=target')['items']
    assert len(result) == 1
    assert 'target evidence' in result[0]['excerpt']
    assert 'text' not in result[0]


@pytest.mark.parametrize('updates', [
    {'workspace_root':''}, {'workspace_root':' '},
    {'projects':{'example':'not-an-object'}}, {'projects':{'example':{}}},
    {'projects':{'example':{'path':None}}}, {'projects':{'example':{'path':' '}}},
    *({'search_aliases':aliases} for aliases in ([], {'a':'b'}, {'a':['']}, {'':['b']})),
])
def test_invalid_config_is_reported_by_doctor_and_rejected_by_web(server, workspace, updates):
    path = workspace.root / 'config.json'
    path.write_text(json.dumps(json.loads(path.read_text()) | updates))
    report = fetch(server, '/api/doctor')
    assert report['state'] == 'error'
    assert report['checks'][0]['state'] == 'error'
    with pytest.raises(HTTPError) as exc:
        urlopen(server + '/api/overview')
    assert exc.value.code == 400


@pytest.mark.parametrize('updates', [{'projects':{'example':{}}}, {'search_aliases':[]}])
def test_web_startup_invalid_config_returns_clean_error(workspace, updates):
    import sys
    path = workspace.root / 'config.json'
    path.write_text(json.dumps(json.loads(path.read_text()) | updates))
    result = subprocess.run([sys.executable, '-m', 'tools.web', '--root', str(workspace.root), '--port', '0'],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 2
    assert ('path' in result.stderr or 'search_aliases' in result.stderr) and 'memory doctor' in result.stderr
    assert 'Traceback' not in result.stderr


def test_web_can_browse_offline_workspace_while_doctor_reports_local_path_unavailable(server, workspace):
    offline = workspace.root.parent / 'offline-workspace'
    offline.write_text('not a local checkout directory')
    config = workspace.root / 'config.json'
    config.write_text(json.dumps(json.loads(config.read_text()) | {'workspace_root':str(offline)}))
    assert fetch(server, '/api/overview')['counts']['events'] == 3
    report = fetch(server, '/api/doctor')
    assert report['checks'][0]['state'] == 'error'
    handoff = fetch(server, '/api/handoff?task=example-task')
    assert handoff['repositories'][0]['available'] is False
