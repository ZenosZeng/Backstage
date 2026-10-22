#!/usr/bin/env python3
"""Filesystem-first shared memory for Codex and Claude Code."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from scripts import retrieval
except ModuleNotFoundError:  # Direct script entry point.
    import retrieval


ROOT = Path(__file__).resolve().parents[1]
EVENT_TYPES = {
    "fact",
    "decision",
    "progress",
    "bugfix",
    "config",
    "risk",
    "hypothesis",
    "test_result",
    "preference",
}
STATUSES = {"active", "superseded", "resolved", "needs_verification"}
TASK_STATES = {"active", "paused", "closed"}
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(root: Path) -> dict[str, Any]:
    path = root / "config.json"
    try:
        config = read_json(path)
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"配置文件必须是 JSON object：{path}")
    for key in ("workspace", "workspace_root", "machine_id"):
        if key not in config:
            raise ValueError(f"配置文件缺少字段：{key}")
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"配置字段 {key} 必须是非空字符串")
    if not SAFE_NAME.fullmatch(config["machine_id"]):
        raise ValueError("machine_id 只能包含字母、数字、点、下划线和连字符")
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("配置字段 projects 必须是 JSON object")
    for item in projects.values():
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip():
            raise ValueError("projects 中每个项目必须配置非空字符串 path")
    retrieval.query_groups('', config.get("search_aliases", {}))
    return config


def workspace_root(config: dict[str, Any]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(config["workspace_root"]))))


def git_value(path: Path, *args: str, timeout: float | None = None, allow_empty: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), *args],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and (value or allow_empty) else None


def project_metadata(config: dict[str, Any], names: list[str], *, timeout: float | None = None) -> list[dict[str, Any]]:
    root = workspace_root(config)
    registry = config.get("projects", {})
    if not isinstance(registry, dict):
        raise ValueError("config.json 中的 projects 必须是 JSON object")
    result: list[dict[str, Any]] = []
    for name in dict.fromkeys(names):
        item = registry.get(name)
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError(f"未知 project：{name}；请先加入 config.json")
        path_hint = str(item["path"])
        path = root / path_hint
        branch = git_value(path, "branch", "--show-current", timeout=timeout) if path.exists() else None
        commit = git_value(path, "rev-parse", "HEAD", timeout=timeout) if path.exists() else None
        dirty = None
        if commit is not None:
            status = git_value(path, "status", "--short", timeout=timeout, allow_empty=True)
            dirty = bool(status) if status is not None else None
        result.append(
            {
                "name": name,
                "path_hint": path_hint,
                "branch": branch,
                "commit": commit,
                "dirty": dirty,
            }
        )
    return result


def event_projects(event: dict[str, Any]) -> list[dict[str, Any]]:
    projects = event.get("projects", event.get("repos", []))
    if not isinstance(projects, list):
        return []
    normalized: list[dict[str, Any]] = []
    for project in projects:
        if isinstance(project, str):
            normalized.append({"name": project})
        elif isinstance(project, dict):
            normalized.append(dict(project))
    return normalized


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("event 必须是 object")
    projects = event.get("projects", event.get("repos", []))
    if not isinstance(projects, list) or any(
        not isinstance(item, (str, dict)) for item in projects
    ):
        raise ValueError("projects/repos 必须是 project name 或 object 的 array")
    value = dict(event)
    value["projects"] = event_projects(value)
    value.pop("repos", None)
    value.setdefault("workspace", "code")
    value.setdefault("scope", "task" if value.get("task_id") else "workspace")
    value.setdefault("sensitivity", "normal")
    value.setdefault("supersedes", [])
    value.setdefault("status", "active")
    value.setdefault("content", {})
    return value


def contains_sensitive_value(value: Any, key: str | None = None) -> bool:
    if key and key.lower().replace("-", "_") in SENSITIVE_KEYS and value not in (None, ""):
        return True
    if isinstance(value, dict):
        return any(contains_sensitive_value(item, str(name)) for name, item in value.items())
    if isinstance(value, list):
        return any(contains_sensitive_value(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    return False


def validate_event(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = (
        "schema_version",
        "event_id",
        "created_at",
        "machine_id",
        "agent",
        "workspace",
        "event_type",
        "topic_key",
        "title",
        "content",
        "status",
        "projects",
    )
    for key in required:
        if key not in event:
            errors.append(f"缺少字段 {key}")
    if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        errors.append("仅支持 schema_version=1")
    for key in ("event_id", "title", "topic_key", "workspace"):
        if not isinstance(event.get(key), str) or not event[key].strip():
            errors.append(f"{key} 必须是非空字符串")
    if not isinstance(event.get("event_type"), str) or event["event_type"] not in EVENT_TYPES:
        errors.append("未知 event_type")
    if not isinstance(event.get("status"), str) or event["status"] not in STATUSES:
        errors.append("未知 status")
    if event.get("event_type") == "hypothesis" and event.get("status") != "needs_verification":
        errors.append("hypothesis 必须使用 needs_verification 状态")
    for key in ("machine_id", "agent"):
        value = event.get(key, "")
        if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
            errors.append(f"{key} 格式无效")
    try:
        parse_time(str(event.get("created_at", "")))
    except ValueError:
        errors.append("created_at 不是有效 ISO-8601 时间")
    if not isinstance(event.get("content"), dict):
        errors.append("content 必须是 object")
    elif "task_state" in event["content"]:
        state = event["content"]["task_state"]
        if not isinstance(state, str) or state not in TASK_STATES:
            errors.append("task_state 必须是 active、paused 或 closed")
        if not isinstance(event.get("task_id"), str) or not event["task_id"].strip():
            errors.append("任务状态事件必须有 task_id")
        if event.get("event_type") != "decision":
            errors.append("任务状态事件必须使用 decision 类型")
        if not event["content"].get("what") or not event["content"].get("where"):
            errors.append("任务状态事件必须记录原因 what 和来源 where")
    if not isinstance(event.get("projects"), list):
        errors.append("projects 必须是 array")
    elif any(
        not isinstance(project, dict)
        or not isinstance(project.get("name"), str)
        or not project["name"].strip()
        for project in event["projects"]
    ):
        errors.append("projects 每项必须是含非空字符串 name 的 object")
    if event.get("task_id") is not None and (
        not isinstance(event["task_id"], str) or not event["task_id"].strip()
    ):
        errors.append("task_id 必须是非空字符串")
    supersedes = event.get("supersedes", [])
    if not isinstance(supersedes, list) or any(
        not isinstance(item, str) or not item for item in supersedes
    ):
        errors.append("supersedes 必须是非空 event_id 的 array")
    elif event.get("event_id") in supersedes:
        errors.append("事件不能 supersede 自己")
    if contains_sensitive_value(event):
        errors.append("事件疑似包含 secret")
    return errors


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(dt.timezone.utc)


def read_daily(path: Path) -> dict[str, Any]:
    document = read_json(path)
    machine, agent, filename = path.parts[-3:]
    dt.date.fromisoformat(filename.removesuffix(".json"))
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise ValueError(f"daily JSON 格式无效：{path}")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1 or (
        document.get("machine_id"), document.get("agent"), document.get("date")
    ) != (machine, agent, filename.removesuffix(".json")):
        raise ValueError(f"daily JSON 身份或日期不匹配：{path}")
    seen = set()
    for raw in document["events"]:
        event = normalize_event(raw)
        errors = validate_event(event)
        if errors:
            raise ValueError(f"daily JSON 事件校验失败：{path}：{'；'.join(errors)}")
        if (event["machine_id"], event["agent"]) != (machine, agent):
            raise ValueError(f"事件与 daily JSON 身份不匹配：{path}")
        if event["event_id"] in seen:
            raise ValueError(f"daily JSON 包含重复 event_id：{path}")
        seen.add(event["event_id"])
    return document


@contextmanager
def operation_lock(root: Path, name: str) -> Iterator[None]:
    path = root / ".local" / ".locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def daily_path(root: Path, event: dict[str, Any]) -> Path:
    date = parse_time(event["created_at"]).date().isoformat()
    return root / ".share" / "memory" / str(event["machine_id"]) / str(event["agent"]) / f"{date}.json"


@contextmanager
def file_lock(root: Path, machine: str, agent: str, date: str) -> Iterator[None]:
    lock_path = root / ".local" / ".locks" / machine / agent / f"{date}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_events(root: Path, events: list[dict[str, Any]]) -> tuple[int, int]:
    # Serialize local imports with pull installation, but never hold this lock during S3 I/O.
    with operation_lock(root, "memory-write"):
        return _append_events(root, events)


def _append_events(root: Path, events: list[dict[str, Any]]) -> tuple[int, int]:
    stored, errors = load_events(root)
    if errors:
        raise ValueError("现有记忆校验失败，请先运行 validate")
    existing = {event["event_id"]: event for event in stored}
    grouped: dict[Path, list[dict[str, Any]]] = {}
    duplicates = 0
    for raw in events:
        event = normalize_event(raw)
        errors = validate_event(event)
        if errors:
            raise ValueError(f"事件校验失败：{'；'.join(errors)}")
        if event["event_id"] in existing:
            if existing[event["event_id"]] != event:
                raise ValueError("event_id 内容冲突，拒绝覆盖；请使用新 ID 和 supersedes")
            duplicates += 1
            continue
        existing[event["event_id"]] = event
        grouped.setdefault(daily_path(root, event), []).append(event)

    reference_errors = lifecycle_reference_errors(list(existing.values()))
    if reference_errors:
        raise ValueError("；".join(item["detail"] for item in reference_errors))

    added = 0
    for path, incoming in grouped.items():
        machine, agent, filename = path.parts[-3:]
        date = filename.removesuffix(".json")
        with file_lock(root, machine, agent, date):
            if path.exists():
                document = read_daily(path)
            else:
                document = {
                    "schema_version": 1,
                    "machine_id": machine,
                    "agent": agent,
                    "date": date,
                    "events": [],
                }
            for event in incoming:
                document["events"].append(event)
                added += 1
            document["events"].sort(key=lambda item: (item["created_at"], item["event_id"]))
            atomic_write_json(path, document)
    return added, duplicates


def memory_files(root: Path) -> list[Path]:
    return sorted((root / ".share" / "memory").glob("*/*/*.json"))


def load_events(
    root: Path, *, sources: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in memory_files(root):
        try:
            document = read_daily(path)
            events.extend(normalize_event(raw) for raw in document["events"])
            if sources is not None:
                sources.update({raw["event_id"]: path.relative_to(root).as_posix()
                                for raw in document["events"]})
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}：{exc}")
    counts = Counter(event["event_id"] for event in events)
    errors.extend(f"重复 event_id：{event_id}" for event_id, count in counts.items() if count > 1)
    events.sort(key=lambda item: (parse_time(item["created_at"]), item["event_id"]))
    return events, errors


def short_text(value: Any, size: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= size else text[:size - 1] + "…"


def render_event(event: dict[str, Any], summary_chars: int | None = None) -> str:
    projects = ", ".join(project.get("name", "?") for project in event["projects"]) or "-"
    what = event.get("content", {}).get("what", "")
    lines = [
        f"[{event['created_at']}] {short_text(event['title'], summary_chars) if summary_chars else event['title']}",
        f"  {event['machine_id']}/{event['agent']} | {event['event_type']}/{event['status']} | {projects}",
        f"  topic={event['topic_key']} task={event.get('task_id') or '-'} id={event['event_id']}",
    ]
    if what:
        lines.append(f"  {short_text(what, summary_chars) if summary_chars else what}")
    return "\n".join(lines)


def supersedes_cycles(events: list[dict[str, Any]]) -> set[str]:
    graph = {event["event_id"]: event.get("supersedes", []) for event in events}
    reverse: dict[str, list[str]] = {key: [] for key in graph}
    for key, targets in graph.items():
        for target in targets:
            if target in reverse:
                reverse[target].append(key)
    # Iterative Kosaraju: linear in records + references, including long chains.
    visited, order = set(), []
    for start in graph:
        pending = [(start, False)]
        while pending:
            key, finished = pending.pop()
            if finished:
                order.append(key)
            elif key in graph and key not in visited:
                visited.add(key)
                pending.append((key, True))
                pending.extend((target, False) for target in graph[key] if target not in visited)
    visited, cyclic = set(), set()
    for start in reversed(order):
        if start in visited:
            continue
        component, pending = set(), [start]
        visited.add(start)
        while pending:
            key = pending.pop()
            component.add(key)
            for target in reverse[key]:
                if target not in visited:
                    visited.add(target)
                    pending.append(target)
        if len(component) > 1 or start in graph[start]:
            cyclic.update(component)
    return cyclic


def select_events(events: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = list(events)
    if getattr(args, "current", False):
        if supersedes_cycles(events):
            raise ValueError("supersedes 存在环，无法筛选当前记忆；请运行 audit 后人工核验")
        superseded = {key for event in events for key in event.get("supersedes", [])}
        selected = [
            event for event in selected
            if event["event_id"] not in superseded and event["status"] != "superseded"
        ]
    for option, key in (("machine", "machine_id"), ("topic", "topic_key")):
        if getattr(args, option, None):
            selected = [event for event in selected if event.get(key) == getattr(args, option)]
    since, until = getattr(args, "since", None), getattr(args, "until", None)
    if since and until and since >= until:
        raise ValueError("--since 必须早于 --until（结束时间不包含在内）")
    if since:
        selected = [event for event in selected if parse_time(event["created_at"]) >= since]
    if until:
        selected = [event for event in selected if parse_time(event["created_at"]) < until]
    if getattr(args, "project", None):
        selected = [
            event
            for event in selected
            if args.project in {project.get("name") for project in event["projects"]}
        ]
    if getattr(args, "task", None):
        selected = [event for event in selected if event.get("task_id") == args.task]
    if getattr(args, "agent", None):
        selected = [event for event in selected if event.get("agent") == args.agent]
    query = getattr(args, "query", None)
    if query:
        terms = retrieval.query_groups(query, getattr(args, "aliases", None))
        selected = [
            event
            for event in selected
            if retrieval.matches(json.dumps(event, ensure_ascii=False), terms)
        ]
    selected.reverse()
    if query and getattr(args, "sort", "recent") == "relevance":
        selected.sort(key=lambda event: relevance_score(event, terms), reverse=True)
    return selected[: args.limit]


def relevance_score(event: dict[str, Any], terms: list[str]) -> int:
    fields = [
        (event["title"], 8), (event["topic_key"], 6),
        (event.get("task_id") or "", 5),
        (" ".join(p.get("name", "") for p in event["projects"]), 4),
        (json.dumps(event["content"], ensure_ascii=False), 1),
    ]
    groups = [(term,) if isinstance(term, str) else term for term in terms]
    return retrieval.score(fields, groups)


def lifecycle_reference_errors(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Validate lifecycle edges against the full set, including cross-machine parents."""
    by_id = {event["event_id"]: event for event in events}
    states = {key: event for key, event in by_id.items() if "task_state" in event["content"]}
    issues = []
    for event in events:
        for parent_id in event.get("supersedes", []):
            parent = by_id.get(parent_id)
            if event["event_id"] in states:
                invalid = parent_id not in states or parent.get("task_id") != event.get("task_id")
            else:
                invalid = parent_id in states
            if invalid:
                issues.append({"kind": "invalid_lifecycle_reference", "event_id": event["event_id"],
                               "detail": f"任务状态引用无效：{event['event_id']} -> {parent_id}；生命周期仅能取代同任务状态事件，普通事件不能取代生命周期"})
    for event_id in sorted(supersedes_cycles(list(states.values()))):
        issues.append({"kind": "lifecycle_cycle", "event_id": event_id,
                       "detail": f"任务状态引用存在环：{event_id}"})
    return issues


def task_states(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project explicit lifecycle heads; ordinary progress never reopens a task."""
    if supersedes_cycles(events):
        raise ValueError("supersedes 存在环，请先运行 audit")
    errors = lifecycle_reference_errors(events)
    if errors:
        raise ValueError("；".join(item["detail"] for item in errors))
    replaced = {key for event in events for key in event.get("supersedes", [])}
    tasks: dict[str, dict[str, Any]] = {}
    for event in events:
        task = event.get("task_id")
        if not task:
            continue
        item = tasks.setdefault(task, {"task_id": task, "state": "unknown", "heads": [], "projects": []})
        item["projects"] = sorted(set(item["projects"]) | {p["name"] for p in event["projects"]})
        if "task_state" not in event["content"]:
            continue
        if event["event_id"] not in replaced and event["status"] != "superseded":
            item["heads"].append(event)
    for item in tasks.values():
        if len(item["heads"]) == 1:
            item["state"] = item["heads"][0]["content"]["task_state"]
        elif len(item["heads"]) > 1:
            item["state"] = "conflict"
    return tasks


def command_task(root: Path, args: argparse.Namespace) -> int:
    if not args.task.strip() or not args.reason.strip() or not all(s.strip() for s in args.where):
        raise ValueError("task、reason 和 where 不得为空白")
    config = load_config(root)
    # Read heads and append under the same existing write lock (same-machine writers).
    with operation_lock(root, "memory-write"):
        events, errors = load_events(root)
        if errors:
            raise ValueError("记忆库校验失败，请先运行 validate")
        tasks = task_states(events)
        current = tasks.get(args.task)
        if current is None and args.state != "active":
            raise ValueError("未知任务；先用 active 建立任务或追加带该 task ID 的记忆")
        heads = current["heads"] if current else []
        if len(heads) > 1 and set(args.resolve) != {e["event_id"] for e in heads}:
            raise ValueError("任务状态冲突；核对后用 --resolve 逐个指定全部 head event_id")
        if args.resolve and set(args.resolve) != {e["event_id"] for e in heads}:
            raise ValueError("--resolve 与当前任务 heads 不一致，请重新读取")
        projects = {p["name"]: {"name": p["name"]}
                    for e in events if e.get("task_id") == args.task for p in e["projects"]}
        registered = [name for name in projects if name in config.get("projects", {})]
        # Capture this decision's checkout, not the commit copied from an old event.
        projects.update({p["name"]: p for p in project_metadata(config, registered + args.project)})
        now = utc_now()
        event = {
            "schema_version": 1,
            "event_id": f"evt_{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{config['machine_id']}_{args.agent}_{uuid.uuid4().hex}",
            "created_at": iso_utc(now), "machine_id": config["machine_id"], "agent": args.agent,
            "workspace": config["workspace"], "scope": "task", "task_id": args.task,
            "projects": list(projects.values()), "event_type": "decision",
            "topic_key": f"task/{args.task}/lifecycle", "title": f"任务 {args.task}: {args.state}",
            "content": {"task_state": args.state, "what": args.reason, "where": args.where},
            "status": "active", "supersedes": [e["event_id"] for e in heads], "sensitivity": "normal",
        }
        _append_events(root, [event])
    print(f"任务 {args.task}: {args.state}；来源 {event['event_id']}")
    return 0


def build_brief(events: list[dict], args: argparse.Namespace) -> dict:
    if not args.project and not args.task:
        raise ValueError("brief 至少指定 --project 或 --task")
    states = task_states(events)
    selectors = argparse.Namespace(**vars(args))
    selectors.current, selectors.limit = True, None
    selected = select_events(events, selectors)
    related = [item for task, item in sorted(states.items())
               if (not args.task or task == args.task)
               and (not args.project or args.project in item["projects"])]
    visible = [item for item in related if args.task or args.include_inactive
               or item["state"] not in {"paused", "closed"}]
    hidden = len(related) - len(visible)
    selected = [e for e in selected if args.task or args.include_inactive
                or states.get(e.get("task_id"), {}).get("state") not in {"paused", "closed"}]
    # Exact duplicate content can be collapsed, but preserve every source ID.
    groups: dict[tuple, dict[str, Any]] = {}
    for event in selected:
        if "task_state" in event["content"]:
            continue
        key = (event.get("task_id"), event["topic_key"], event["event_type"], event["status"],
               json.dumps(event["content"], ensure_ascii=False, sort_keys=True))
        if key not in groups:
            groups[key] = {"event": event, "sources": []}
        groups[key]["sources"].append(event["event_id"])
    sections: dict[str, list] = {name: [] for name in ("约定与决策", "事实与验证记录", "当前进展", "风险与待验证", "待办候选")}
    buckets: dict[str, list] = {name: [] for name in sections if name != "待办候选"}
    for group in groups.values():
        event = group["event"]
        kind = event["event_type"]
        section = ("风险与待验证" if kind in {"risk", "hypothesis"} or event["status"] == "needs_verification"
                   else "约定与决策" if kind in {"decision", "preference", "config"}
                   else "当前进展" if kind == "progress" else "事实与验证记录")
        buckets[section].append(group)
    # Share the budget across categories so fresh progress cannot bury constraints.
    picked = []
    for index in range(max((len(b) for b in buckets.values()), default=0)):
        for section, bucket in buckets.items():
            if index < len(bucket) and len(picked) < args.limit:
                picked.append((section, bucket[index]))
        if len(picked) >= args.limit:
            break
    for section, group in picked:
        event = group["event"]
        kind = event["event_type"]
        entry = {"title": short_text(event["title"], args.summary_chars),
                 "text": short_text(event["content"].get("what", ""), args.summary_chars),
                 "sources": group["sources"], "created_at": event["created_at"],
                 "agent": event["agent"], "machine": event["machine_id"],
                 "projects": [p["name"] for p in event["projects"]],
                 "type": kind, "status": event["status"], "task_id": event.get("task_id")}
        sections[section].append(entry)
        task = event.get("task_id")
        if task and states[task]["state"] in {"active", "unknown"} and event["status"] == "active":
            todo = event["content"].get("next", [])
            if todo:
                sections["待办候选"].append({**entry, "text": short_text(todo, args.summary_chars)})
    result = {
        "notice": "仅摘录记忆，不裁决事实；同 topic 并列不表示一致。unknown 任务尚未声明生命周期；待办需核对源码与现场。",
        "tasks": [{"task_id": item["task_id"], "state": item["state"], "projects": item["projects"],
                   "heads": [{"event_id": e["event_id"], "state": e["content"]["task_state"],
                              "created_at": e["created_at"], "agent": e["agent"], "machine": e["machine_id"],
                              "reason": short_text(e["content"]["what"], args.summary_chars),
                              "where": e["content"]["where"]} for e in item["heads"]]} for item in visible],
        "hidden_inactive_tasks": hidden, "omitted_events": max(0, len(groups) - args.limit), "sections": sections,
    }
    return result


def render_brief(report: dict, max_chars: int = 6000) -> str:
    """Pack whole records across categories before deepening their excerpts."""
    notice = report["notice"]
    footer = "\n[输出已截断：按类别保留完整条目；更多来源可用 --json 或 get <event_id> 查看。]"
    header = f"{notice}\n隐藏暂停/关闭任务 {report['hidden_inactive_tasks']}；未展示记录组 {report['omitted_events']}"
    if len(header) + len(footer) > max_chars:
        return ("记忆摘要空间不足，请增大 --max-chars 或使用 --json。" + footer)[:max_chars]
    tasks = sorted(report["tasks"], key=lambda t: (t["state"] not in {"conflict", "active"}, t["task_id"]))
    task_budget = min(1000, max_chars // 5, max(0, max_chars-len(header)-len(footer)-4))
    remainder = f"其余 {len(tasks)} 个任务；完整状态依据见 --task <id> --json。"
    content_budget = max(0, task_budget-len(remainder)-1)
    task_blocks, task_details = [], []
    for task in tasks[:8]:
        line = f"任务 {task['task_id']}: {task['state']}"
        minimal = [line] + [f"  [{h['event_id']}] {h['state']}" for h in task['heads']]
        detailed = [line] + [f"  [{h['event_id']}] {h['state']}: {h['reason']}\n    依据: {short_text(h['where'], 100)}"
                             for h in task['heads']]
        block = "\n".join(minimal)
        if len("\n".join(task_blocks + [block])) > content_budget:
            continue
        task_blocks.append(block)
        task_details.append("\n".join(detailed))
    shown_tasks = len(task_blocks)
    for index, detail in enumerate(task_details):
        previous = task_blocks[index]
        task_blocks[index] = detail
        if len("\n".join(task_blocks)) > content_budget:
            task_blocks[index] = previous
    task_text = "\n".join(task_blocks)
    if len(tasks) > shown_tasks:
        remainder = f"其余 {len(tasks)-shown_tasks} 个任务；完整状态依据见 --task <id> --json。"
        if len(task_text) + len(remainder) + 1 <= task_budget:
            task_text += ("\n" if task_text else "") + remainder
    sections = report["sections"]
    chosen = {name: [] for name in sections}
    def block(entry, deep=False):
        sources = entry["sources"]
        source = ", ".join(sources) if deep else sources[0] + (f"（另 {len(sources)-1} 个来源）" if len(sources) > 1 else "")
        return f"- {entry['title'] if deep else short_text(entry['title'], 48)} ({entry['type']}/{entry['status']})\n  {entry['text'] if deep else short_text(entry['text'], 80)}\n  来源: {source}"
    def assemble():
        parts = [header]
        parts += [name + "\n" + "\n".join(values) for name, values in chosen.items() if values]
        if task_text:
            parts.append(task_text)
        return "\n\n".join(parts)
    positions = []
    for index in range(max((len(v) for v in sections.values()), default=0)):
        for name, entries in sections.items():
            if index >= len(entries):
                continue
            entry = entries[index]
            chosen[name].append(block(entry))
            if len(assemble()) + len(footer) > max_chars:
                chosen[name].pop()
            else:
                positions.append((name, len(chosen[name])-1, entry))
    for name, index, entry in positions:
        old = chosen[name][index]
        chosen[name][index] = block(entry, deep=True)
        if len(assemble()) + len(footer) > max_chars:
            chosen[name][index] = old
    omitted = (len(tasks) > shown_tasks or report["omitted_events"] > 0
               or len(positions) < sum(len(v) for v in sections.values())
               or any(chosen[name][index] != block(entry, deep=True) for name, index, entry in positions)
               or task_blocks != task_details)
    return assemble() + (footer if omitted else "")


def command_brief(root: Path, args: argparse.Namespace) -> int:
    events, errors = load_events(root)
    if errors:
        raise ValueError("记忆库校验失败，请先运行 validate")
    result = build_brief(events, args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        # Include the final newline in the text budget, without slicing records.
        output = render_brief(result, max(0, args.max_chars - 1))
        print(output)
    return 0


def handoff(root: Path, events: list[dict], task_id: str, *, limit: int = 5, sources=None) -> dict:
    """A repeatable read, never a claim, lifecycle transition or execution request."""
    tasks = task_states(events)
    if task_id not in tasks:
        raise FileNotFoundError("任务不存在")
    task = tasks[task_id]
    args = argparse.Namespace(task=task_id, current=True, limit=None)
    selected = [e for e in select_events(events, args) if "task_state" not in e["content"]]
    sections = {name: [] for name in ("当前进展", "约定与决策", "失败与修复记录", "验证结果", "事实与依据", "未决问题", "后续候选")}
    def entry(event, text=None):
        return {"event_id": event["event_id"], "title": event["title"], "created_at": event["created_at"],
                "type": event["event_type"], "status": event["status"],
                "text": text if text is not None else short_text(event["content"].get("what", ""), 600),
                "where": event["content"].get("where", []),
                "verified_by": event["content"].get("verified_by", []),
                "source": (sources or {}).get(event["event_id"]),
                "projects": event["projects"]}
    for e in selected:
        kind = e["event_type"]
        section = ("当前进展" if kind == "progress" else "约定与决策" if kind in {"decision", "preference", "config"}
                   else "失败与修复记录" if kind in {"bugfix", "risk"}
                   else "验证结果" if kind == "test_result" else "事实与依据")
        if kind != "hypothesis":
            sections[section].append(entry(e))
        if kind in {"risk", "hypothesis"} and e["status"] in {"active", "needs_verification"}:
            sections["未决问题"].append(entry(e))
        if task["state"] in {"active", "unknown"} and e["status"] == "active" and e["content"].get("next"):
            sections["后续候选"].append(entry(e, short_text(e["content"]["next"], 600)))
    config = load_config(root)
    registered = [name for name in task["projects"] if name in config.get("projects", {})]
    repositories = project_metadata(config, registered, timeout=3)
    for repo in repositories:
        repo["available"] = repo["commit"] is not None
        if not repo["available"]:
            repo["dirty"] = None
    return {"task_id": task_id, "state": task["state"], "heads": task["heads"],
            "notice": "只读交接；候选不是执行授权。记忆中的分支与提交是历史快照，当前仓库状态另列；关闭/暂停/冲突任务不推荐旧待办。",
            "repositories": repositories,
            "unregistered_repositories": sorted(set(task["projects"]) - set(registered)),
            "sections": {name: rows[:limit] for name, rows in sections.items()},
            "omitted": {name: max(0, len(rows)-limit) for name, rows in sections.items()}}


def command_handoff(root: Path, args: argparse.Namespace) -> int:
    sources = {}
    events, errors = load_events(root, sources=sources)
    if errors:
        raise ValueError("记忆库校验失败，请先运行 validate")
    try:
        result = handoff(root, events, args.task, limit=args.limit, sources=sources)
    except FileNotFoundError:
        print("任务不存在", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"任务 {result['task_id']}: {result['state']}\n{result['notice']}")
        print("\n任务状态依据")
        for head in result["heads"]:
            print(f"- [{head['event_id']}] {head['content']['task_state']}: {head['content']['what']}")
            print("  依据: " + short_text(head['content']['where'], 1000))
        if not result["heads"]:
            print("暂无显式生命周期记录。")
        for name, rows in result["sections"].items():
            if rows:
                print("\n" + name)
            for row in rows:
                print(f"- {row['title']} ({row['type']}/{row['status']})\n  {row['text']}\n  来源: {row['event_id']} ({row['source']})")
            if result["omitted"][name]:
                print(f"另 {result['omitted'][name]} 条，见此任务的全部记忆。")
        for repo in result["repositories"]:
            dirty = "不可用" if repo['dirty'] is None else "有未提交改动" if repo['dirty'] else "干净"
            print(f"\n当前仓库 {repo['name']}: {repo['branch'] or '分支不可用'} / {repo['commit'] or '提交不可用'} / 改动状态: {dirty}")
        if result["unregistered_repositories"]:
            print("\n本机未注册仓库: " + ", ".join(result["unregistered_repositories"]))
    return 0


def command_doctor(root: Path, args: argparse.Namespace) -> int:
    try:
        from scripts.doctor import inspect_workspace
    except ModuleNotFoundError:
        from doctor import inspect_workspace
    report = inspect_workspace(root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report["checks"]:
            print(f"{check['state']}: {check['name']} — {check['detail']}")
            if check.get("hint"):
                print("  " + check["hint"])
        code = report["source_code"]
        print(f"\n本机核心: {code['version'] or '未知'} · {code['commit'] or '提交未知'} · 改动状态: {code['dirty']}")
        for skill in report["skills"]:
            print(f"Skill {skill['name']}: {skill['state']} · 最低核心 {skill['minimum_version'] or '未声明'}")
        print("\n本机状态目录: " + report["local_root"])
        for event in report["last_events"]:
            print(f"最后记忆 {event['machine']}/{event['agent']}: {event['created_at']}")
        print(report["notice"])
    return 2 if report["state"] == "error" else 1 if report["state"] == "warning" else 0


def command_add(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    now = utc_now()
    status = args.status
    if args.event_type == "hypothesis" and status == "active":
        status = "needs_verification"
    event = {
        "schema_version": 1,
        "event_id": f"evt_{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{config['machine_id']}_{args.agent}_{uuid.uuid4().hex}",
        "created_at": iso_utc(now),
        "machine_id": config["machine_id"],
        "agent": args.agent,
        "workspace": config["workspace"],
        "scope": "task" if args.task else ("repo" if args.project else "workspace"),
        "task_id": args.task,
        "projects": project_metadata(config, args.project),
        "event_type": args.event_type,
        "topic_key": args.topic,
        "title": args.title,
        "content": {
            "what": args.what,
            "why": args.why,
            "where": args.where,
            "verified_by": args.verified_by,
            "next": args.next,
        },
        "status": status,
        "supersedes": args.supersedes,
        "sensitivity": "normal",
    }
    added, _ = append_events(root, [event])
    print(f"已记录 {added} 条事件：{event['event_id']}")
    print(daily_path(root, event))
    return 0


def command_import(root: Path, args: argparse.Namespace) -> int:
    document = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if isinstance(document, dict) and isinstance(document.get("events"), list):
        events = document["events"]
    elif isinstance(document, list):
        events = document
    else:
        raise ValueError("导入文件必须是 event array 或包含 events array 的 object")
    added, duplicates = append_events(root, events)
    print(f"导入完成：新增 {added}，重复 {duplicates}")
    return 0


def command_list(root: Path, args: argparse.Namespace) -> int:
    args.aliases = {}
    if getattr(args, "query", None) and args.query.strip():
        # Recovery reads need only optional aliases, not workspace identity or Git config.
        try:
            config = read_json(root / "config.json")
            if not isinstance(config, dict):
                raise ValueError("配置必须是 object")
            args.aliases = config.get("search_aliases", {})
            retrieval.query_groups('', args.aliases)
        except (OSError, ValueError) as error:
            args.aliases = {}
            if not isinstance(error, FileNotFoundError) or (root / "config.json").is_symlink():
                print("警告：配置或搜索别名不可用；本次按原词检索，未应用别名。", file=sys.stderr)
    if getattr(args, "documents", False):
        if any(getattr(args, key, None) for key in ("project", "task", "agent", "machine", "topic", "since", "until")) or args.current:
            raise ValueError("文档搜索不使用事件筛选；业务范围请从工作台文档中心选择")
        selected = retrieval.search_documents(retrieval.scan_documents(root), args.query,
                                             aliases=args.aliases, sort=args.sort)[:args.limit]
        if args.json:
            print(json.dumps(selected, ensure_ascii=False, indent=2))
        else:
            print("\n\n".join(f"{d['title']}\n  {d['path']}\n  {d.get('excerpt', '')}" for d in selected) or "没有匹配的文档。")
        return 0
    events, errors = load_events(root)
    if errors:
        print("记忆库包含无效文件，请先运行 validate", file=sys.stderr)
        return 2
    selected = select_events(events, args)
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    elif selected:
        size = args.summary_chars if getattr(args, "summary", False) else None
        print("\n\n".join(render_event(event, size) for event in selected))
    else:
        print("没有匹配的记忆。")
    return 0


def command_status(root: Path, _args: argparse.Namespace) -> int:
    config = load_config(root)
    events, errors = load_events(root)
    by_machine = Counter(event.get("machine_id", "?") for event in events)
    by_agent = Counter(event.get("agent", "?") for event in events)
    print(f"Workspace: {config['workspace']} ({workspace_root(config)})")
    print(f"Current machine: {config['machine_id']} ({config.get('machine_role', '-')})")
    print(f"Daily files: {len(memory_files(root))}")
    print(f"Events: {len(events)}")
    print(f"By machine: {dict(sorted(by_machine.items()))}")
    print(f"By agent: {dict(sorted(by_agent.items()))}")
    print(f"Validation errors: {len(errors)}")
    return 1 if errors else 0


def command_validate(root: Path, _args: argparse.Namespace) -> int:
    events, errors = load_events(root)
    errors.extend(item["detail"] for item in lifecycle_reference_errors(events))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"校验失败：{len(events)} 条事件，{len(errors)} 个错误", file=sys.stderr)
        return 1
    print(f"校验通过：{len(memory_files(root))} 个 daily JSON，{len(events)} 条事件")
    return 0


def command_get(root: Path, args: argparse.Namespace) -> int:
    sources: dict[str, str] = {}
    events, errors = load_events(root, sources=sources)
    if errors:
        raise ValueError("记忆库校验失败，请先运行 validate")
    requested = list(dict.fromkeys(args.event_id))
    if len(requested) > 20:
        raise ValueError("一次最多读取 20 个 event_id")
    by_id = {e["event_id"]: e for e in events}
    missing = [key for key in requested if key not in by_id]
    items = [{"source": sources[key], "event": by_id[key]} for key in requested if key in by_id]
    if len(args.event_id) == 1:
        if missing:
            print("没有找到该 event_id。", file=sys.stderr)
            return 1
        result = items[0]  # 保留原有单条 get 的输出格式
    else:
        result = {"items": items, "missing": missing}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if missing else 0


def event_context(events, sources, event_id, *, before=3, after=3) -> dict:
    """Small chronological index around an anchor; full evidence stays in get."""
    if not 0 <= before <= 10 or not 0 <= after <= 10:
        raise ValueError("before / after 必须在 0–10 之间")
    by_id = {e["event_id"]: e for e in events}
    if event_id not in by_id:
        raise FileNotFoundError("记忆不存在")
    anchor = by_id[event_id]
    projects = {p["name"] for p in anchor["projects"]}
    task = anchor.get("task_id")
    def same_scope(e):
        if task:
            return e.get("task_id") == task
        return (not e.get("task_id") and e["workspace"] == anchor["workspace"]
                and e["topic_key"] == anchor["topic_key"]
                and {p["name"] for p in e["projects"]} == projects)
    timeline = sorted((e for e in events if same_scope(e)),
                      key=lambda e: (parse_time(e["created_at"]), e["event_id"]))
    index = next(i for i, e in enumerate(timeline) if e["event_id"] == event_id)
    neighbors = timeline[max(0, index-before):index+after+1]
    reverse: dict[str, list[str]] = {}
    for e in events:
        for target in e.get("supersedes", []):
            reverse.setdefault(target, []).append(e["event_id"])
    # 修正链优先于普通邻居；广度遍历只列 20 条，环也不会无限展开。
    selected, pending = {event_id}, [event_id]
    cursor = 0
    while cursor < len(pending) and len(selected) < 20:
        key = pending[cursor]
        cursor += 1
        for related in by_id[key].get("supersedes", []) + reverse.get(key, []):
            if related in by_id and related not in selected:
                selected.add(related)
                pending.append(related)
                if len(selected) == 20:
                    break
    for e in sorted(neighbors, key=lambda e: abs(timeline.index(e)-index)):
        if len(selected) < 20:
            selected.add(e["event_id"])
    items = []
    for e in sorted((by_id[key] for key in selected), key=lambda e: (parse_time(e["created_at"]), e["event_id"])):
        key = e["event_id"]
        items.append({"event_id": key, "title": short_text(e["title"], 160),
                      "created_at": e["created_at"], "event_type": e["event_type"], "status": e["status"],
                      "source": sources[key], "excerpt": short_text(e["content"].get("what", ""), 240),
                      "anchor": key == event_id, "correction": key in pending and key != event_id,
                      "replaced": bool(reverse.get(key)) or e["status"] == "superseded",
                      "supersedes": e.get("supersedes", [])[:20], "replaced_by": reverse.get(key, [])[:20],
                      "omitted_references": max(0, len(e.get("supersedes", []))-20) + max(0, len(reverse.get(key, []))-20)})
    warnings = []
    try:
        task_state = task_states(events).get(task, {}).get("state", "unknown")
    except ValueError:
        task_state = "unknown"
        warnings.append("存在修正环或生命周期引用错误，任务状态无法裁决；请运行 audit。")
    return {"anchor_id": event_id, "scope": {"task_id": task, "topic_key": None if task else anchor["topic_key"]},
            "task_state": task_state, "warnings": warnings,
            "items": items, "scope_total": len(timeline),
            "omitted_neighbors": sum(e["event_id"] not in selected for e in timeline),
            "corrections_truncated": any(ref not in selected for key in pending
                for ref in by_id[key].get("supersedes", []) + reverse.get(key, []) if ref in by_id),
            "notice": "历史上下文仅供核对；被取代记录与后续候选不构成当前事实或执行授权。完整证据用 get 按 ID 读取。"}


def command_context(root: Path, args: argparse.Namespace) -> int:
    sources = {}
    events, errors = load_events(root, sources=sources)
    if errors:
        raise ValueError("记忆库校验失败，请先运行 validate")
    report = event_context(events, sources, args.event_id, before=args.before, after=args.after)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    text = f"事件上下文 {report['anchor_id']} · 任务状态 {report['task_state']}\n{report['notice']}\n"
    text += '\n'.join(report['warnings'])
    rendered = set()
    def omission_notice(ids):
        omitted = report['omitted_neighbors'] + len(report['items']) - len(ids)
        corrections_omitted = report['corrections_truncated'] or any(
            (item['anchor'] or item['correction']) and item['event_id'] not in ids
            for item in report['items'])
        return f"\n未展示邻域/预算记录 {omitted}；修正链{'有省略' if corrections_omitted else '已列出'}。"
    if len(text) + len(omission_notice(rendered)) > args.max_chars:
        text = short_text("预算不足；请增大 --max-chars。", args.max_chars)
    else:
        # 先保留锚点和修正链，再读取普通邻居；只省略整条，不截断来源 ID。
        for item in sorted(report['items'], key=lambda e: (not e['anchor'], not e['correction'])):
            block = f"\n[{item['created_at']}] {item['title']}（{'已被取代' if item['replaced'] else item['status']}）\n{item['excerpt']}\n来源: {item['event_id']} · {item['source']}\n"
            selected = rendered | {item['event_id']}
            if len(text) + len(block) + len(omission_notice(selected)) <= args.max_chars:
                text += block
                rendered = selected
        text += omission_notice(rendered)
    print(text)
    return 0


def audit_events(events, errors, sources, args) -> dict:
    issues = [{"kind": "invalid", "detail": error} for error in errors]
    known = {event["event_id"] for event in events}
    superseded = {key for event in events for key in event.get("supersedes", [])}
    cyclic = supersedes_cycles(events)
    replacements: dict[str, list[str]] = {}
    for event in events:
        for key in event.get("supersedes", []):
            if event["event_id"] not in superseded:
                replacements.setdefault(key, []).append(event["event_id"])
    ambiguous = {key for values in replacements.values() if len(values) > 1 for key in values}
    cutoff = utc_now() - dt.timedelta(days=args.stale_days)
    selected = select_events(events, args)
    selected_ids = {event["event_id"] for event in selected}
    lifecycle_errors = lifecycle_reference_errors(events)
    issues.extend({**item, "source": sources[item["event_id"]]}
                  for item in lifecycle_errors if item["event_id"] in selected_ids)
    if not lifecycle_errors and not cyclic:
        for task in task_states(events).values():
            if task["state"] == "conflict" and any(e["event_id"] in selected_ids for e in task["heads"]):
                issues.append({"kind": "task_state_conflict", "task_id": task["task_id"],
                               "detail": f"任务 {task['task_id']} 有多个有效状态 head",
                               "heads": [e["event_id"] for e in task["heads"]]})
    for event in selected:
        kinds = []
        content = event["content"]
        if (
            not content.get("verified_by") and not content.get("where")
            and not any(p.get("commit") for p in event["projects"])
        ):
            kinds.append("missing_source")
        if any(key not in known for key in event.get("supersedes", [])):
            kinds.append("dangling_supersedes")
        if event["event_id"] in ambiguous:
            kinds.append("parallel_supersedes_candidate")
        if event["event_id"] in cyclic:
            kinds.append("supersedes_cycle")
        if Path(sources[event["event_id"]]).stem != parse_time(event["created_at"]).date().isoformat():
            kinds.append("legacy_date_mismatch")
        if event["created_at"][-6:] != "+00:00" and not event["created_at"].endswith("Z"):
            kinds.append("legacy_non_utc")
        if (
            event["event_id"] not in superseded
            and event["status"] in {"active", "needs_verification"}
            and event["event_type"] in {"progress", "risk", "hypothesis"}
            and parse_time(event["created_at"]) < cutoff
        ):
            kinds.append("stale_candidate")
        for kind in kinds:
            issues.append({
                "kind": kind, "event_id": event["event_id"],
                "source": sources[event["event_id"]],
            })
    result = {
        "checked_events": len(selected), "issues": issues,
        "counts": dict(Counter(item["kind"] for item in issues)),
    }
    return result


def command_audit(root: Path, args: argparse.Namespace) -> int:
    sources = {}
    events, errors = load_events(root, sources=sources)
    result = audit_events(events, errors, sources, args)
    issues = result["issues"]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"检查 {result['checked_events']} 条事件；仅列候选问题，不自动删除或判定失效。")
        for item in issues:
            print(f"{item['kind']}: {item.get('event_id', item.get('detail', ''))}")
        print(f"汇总：{result['counts']}")
    return 1 if issues else 0


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def filter_time(value: str) -> dt.datetime:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return parse_time(value + "T00:00:00Z")
        return parse_time(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("使用 YYYY-MM-DD（UTC）或带时区的 ISO-8601 时间") from exc


def add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project")
    parser.add_argument("--task")
    parser.add_argument("--agent")
    parser.add_argument("--machine")
    parser.add_argument("--topic", help="精确 topic_key")
    parser.add_argument("--since", type=filter_time, help="包含起点，日期按 UTC")
    parser.add_argument("--until", type=filter_time, help="不包含终点，日期按 UTC")
    parser.add_argument("--current", action="store_true", help="排除显式被取代事件，不自动判断同 topic 的正误")
    parser.add_argument("--limit", type=positive_int, default=20)
    parser.add_argument("--json", action="store_true")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="简化的跨 Agent、跨机器工作区记忆工具")
    result.add_argument("--root", type=Path, default=ROOT, help=".agents 目录")
    subparsers = result.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="原子追加一条事件")
    add.add_argument("--agent", required=True, choices=("codex", "claude", "kimi", "dsh"))
    add.add_argument("--project", action="append", default=[])
    add.add_argument("--task")
    add.add_argument("--type", dest="event_type", required=True, choices=sorted(EVENT_TYPES))
    add.add_argument("--status", choices=sorted(STATUSES), default="active")
    add.add_argument("--topic", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--what", required=True)
    add.add_argument("--why", default="")
    add.add_argument("--where", action="append", default=[])
    add.add_argument("--verified-by", action="append", default=[])
    add.add_argument("--next", action="append", default=[])
    add.add_argument("--supersedes", action="append", default=[])
    add.set_defaults(func=command_add)

    search = subparsers.add_parser("search", help="搜索原始记忆")
    search.add_argument("query")
    search.add_argument("--documents", action="store_true", help="搜索允许目录中的文档标题、路径与 Markdown 正文")
    add_filters(search)
    search.add_argument("--sort", choices=("relevance", "recent"), default="relevance")
    search.add_argument("--summary-chars", type=positive_int, default=240)
    search_format = search.add_mutually_exclusive_group()
    search_format.add_argument("--summary", dest="summary", action="store_true")
    search_format.add_argument("--full", dest="summary", action="store_false")
    search.set_defaults(func=command_list, summary=True)

    recent = subparsers.add_parser("recent", help="读取最近记忆")
    add_filters(recent)
    recent.add_argument("--summary", action="store_true")
    recent.add_argument("--summary-chars", type=positive_int, default=240)
    recent.set_defaults(func=command_list)

    task = subparsers.add_parser("task", help="追加任务生命周期决定，不改历史记录")
    task.add_argument("--task", required=True, help="工作区内唯一、跨 repo 稳定 task ID")
    task.add_argument("--state", required=True, choices=sorted(TASK_STATES))
    task.add_argument("--agent", required=True, choices=("codex", "claude", "kimi", "dsh"))
    task.add_argument("--project", action="append", default=[])
    task.add_argument("--reason", required=True)
    task.add_argument("--where", action="append", required=True, help="决定依据/来源")
    task.add_argument("--resolve", action="append", default=[], help="明确取代的冲突 head ID；需列全")
    task.set_defaults(func=command_task)

    brief = subparsers.add_parser("brief", help="按项目或任务生成有来源的精简上下文")
    add_filters(brief)
    brief.add_argument("--include-inactive", action="store_true", help="包含暂停/关闭任务的历史，不推荐其待办")
    brief.add_argument("--summary-chars", type=positive_int, default=240)
    brief.add_argument("--max-chars", type=positive_int, default=6000, help="文本总字符上限；JSON 不截断")
    brief.set_defaults(func=command_brief, current=True)

    import_events = subparsers.add_parser("import", help="幂等导入 event array")
    import_events.add_argument("--input", required=True)
    import_events.set_defaults(func=command_import)

    status = subparsers.add_parser("status", help="显示记忆库状态")
    status.set_defaults(func=command_status)

    validate = subparsers.add_parser("validate", help="校验全部 daily JSON")
    validate.set_defaults(func=command_validate)

    get = subparsers.add_parser("get", help="按 ID 读取完整事件及来源文件")
    get.add_argument("event_id", nargs="+")
    get.set_defaults(func=command_get)

    context = subparsers.add_parser("context", help="围绕事件读取同任务邻域与修正链，再按 ID get 完整证据")
    context.add_argument("event_id")
    context.add_argument("--before", type=int, default=3)
    context.add_argument("--after", type=int, default=3)
    context.add_argument("--max-chars", type=positive_int, default=6000, help="文本字符预算；JSON 按事件数限制")
    context.add_argument("--json", action="store_true")
    context.set_defaults(func=command_context)

    hand = subparsers.add_parser("handoff", help="按任务生成可重复读取的交接视图")
    hand.add_argument("--task", required=True)
    hand.add_argument("--limit", type=positive_int, default=5, help="每类最多条数")
    hand.add_argument("--json", action="store_true")
    hand.set_defaults(func=command_handoff)

    doctor = subparsers.add_parser("doctor", help="本机只读自检；不联网、不安装或修复链接")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=command_doctor)

    audit = subparsers.add_parser("audit", help="只读检查来源、过期候选及 supersedes 引用")
    add_filters(audit)
    audit.add_argument("--stale-days", type=positive_int, default=60)
    audit.set_defaults(func=command_audit, limit=None)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args.root.resolve(), args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
