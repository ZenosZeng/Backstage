"""Task-linked session metadata and explicit, expiring execution observations.

This module never opens native Agent sessions or starts an Agent. Lifecycle
decisions describe a task; only an observation can describe an executing session.
"""
from __future__ import annotations

import datetime as dt
import math
import re


AGENTS = {"codex", "claude", "kimi", "dsh"}
RUNTIME_STATES = {"running", "waiting", "idle", "error"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_BUSINESS_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _time(value):
    if not isinstance(value, str):
        raise ValueError("时间必须是含时区的 ISO-8601 字符串")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(dt.timezone.utc)


def _text(value):
    return (isinstance(value, str) and bool(value.strip())
            and not any(ord(char) < 32 for char in value))


def _identity_errors(value, name):
    if not isinstance(value, dict):
        return [f"{name} 必须是 object"]
    errors = []
    if not isinstance(value.get("agent"), str) or value["agent"] not in AGENTS:
        errors.append(f"{name}.agent 必须是 codex、claude、kimi 或 dsh")
    if not _text(value.get("session_id")) or value["session_id"] != value["session_id"].strip():
        errors.append(f"{name}.session_id 必须是非空、无首尾空白或控制字符的字符串")
    machine = value.get("machine_id")
    if not isinstance(machine, str) or not _SAFE_NAME.fullmatch(machine):
        errors.append(f"{name}.machine_id 格式无效")
    for key in ("repository", "worktree"):
        if key in value and not _text(value[key]):
            errors.append(f"{name}.{key} 必须是非空、无控制字符的字符串")
    return errors


def validate_context(event):
    """Validate only optional new fields; old event schemas stay readable."""
    content = event.get("content")
    if not isinstance(content, dict):
        return []
    errors = []
    if "business_projects" in content:
        names = content["business_projects"]
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not _BUSINESS_SLUG.fullmatch(name) for name in names
        ):
            errors.append("content.business_projects 必须是业务项目 slug 的 array")
    if "session_refs" in content or "agent_runtime" in content:
        task = event.get("task_id")
        if not isinstance(task, str) or not task.strip() or task.strip() == "-":
            errors.append("会话关联和运行观测必须有有效 task_id")
    if "session_refs" in content:
        refs = content["session_refs"]
        if not isinstance(refs, list):
            errors.append("content.session_refs 必须是 array")
        else:
            for ref in refs:
                errors.extend(_identity_errors(ref, "content.session_refs"))
    if "agent_runtime" in content:
        runtime = content["agent_runtime"]
        errors.extend(_identity_errors(runtime, "content.agent_runtime"))
        if isinstance(runtime, dict):
            if not isinstance(runtime.get("state"), str) or runtime["state"] not in RUNTIME_STATES:
                errors.append("content.agent_runtime.state 必须是 running、waiting、idle 或 error")
            try:
                _time(runtime.get("observed_at"))
            except (ValueError, OverflowError):
                errors.append("content.agent_runtime.observed_at 必须是含时区的 ISO-8601 时间")
        where = content.get("where")
        if not (_text(where) or isinstance(where, list) and bool(where) and all(_text(v) for v in where)):
            errors.append("运行观测必须有 content.where 来源")
    return errors


def _current(events, task_id):
    replaced = {key for event in events for key in event.get("supersedes", [])}
    return [event for event in events if event.get("task_id") == task_id
            and event["event_id"] not in replaced and event.get("status") != "superseded"]


def _key(identity):
    return tuple(identity[name] for name in ("machine_id", "agent", "session_id"))


def _identity(value):
    return {key: value[key] for key in ("agent", "session_id", "machine_id", "repository", "worktree")
            if key in value}


def session_references(events, task_id, sources=None):
    """Return current task/session links with deduplicated evidence, not chats."""
    sources = sources or {}
    result = {}
    for event in sorted(_current(events, task_id), key=lambda e: (_time(e["created_at"]), e["event_id"])):
        content = event["content"]
        refs = list(content.get("session_refs", []))
        if "agent_runtime" in content:
            refs.append(content["agent_runtime"])
        for ref in refs:
            key = _key(ref)
            row = result.setdefault(key, {"event_ids": [], "evidence": []})
            row.update(_identity(ref), last_seen_at=event["created_at"],
                       event_id=event["event_id"], source=sources.get(event["event_id"]))
            if event["event_id"] not in row["event_ids"]:
                row["event_ids"].append(event["event_id"])
                row["evidence"].append({"event_id": event["event_id"],
                                        "source": sources.get(event["event_id"]),
                                        "created_at": event["created_at"]})
    return sorted(result.values(), key=lambda row: (_time(row["last_seen_at"]), _key(row)), reverse=True)


def execution_status(events, task_id, *, now=None, fresh_seconds=300, sources=None):
    """Project latest observations per session; stale/future/conflicting is unknown.

    Several sessions can execute one task. Any fresh running observation keeps
    the task running; otherwise waiting, error and idle follow in that order.
    Full per-session states remain available so aggregation cannot hide errors.
    """
    if (type(fresh_seconds) not in (int, float) or not math.isfinite(fresh_seconds)
            or fresh_seconds <= 0):
        raise ValueError("fresh_seconds 必须大于 0")
    now = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(now, str):
        now = _time(now)
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise ValueError("now 必须是含时区的时间")
    sources = sources or {}
    grouped = {}
    for event in _current(events, task_id):
        runtime = event["content"].get("agent_runtime")
        if runtime:
            grouped.setdefault(_key(runtime), []).append(event)
    sessions = []
    for related in grouped.values():
        latest_time = max(_time(e["content"]["agent_runtime"]["observed_at"]) for e in related)
        heads = [e for e in related if _time(e["content"]["agent_runtime"]["observed_at"]) == latest_time]
        event = max(heads, key=lambda e: (_time(e["created_at"]), e["event_id"]))
        runtime = event["content"]["agent_runtime"]
        age = (now - latest_time).total_seconds()
        reason = ("future_observation" if age < 0 else "stale" if age > fresh_seconds
                  else "conflicting_observations" if len({e["content"]["agent_runtime"]["state"] for e in heads}) > 1
                  else None)
        sessions.append({"state": "unknown" if reason else runtime["state"],
                         "observed_state": runtime["state"], "observed_at": runtime["observed_at"],
                         "event_id": event["event_id"], "source": sources.get(event["event_id"]),
                         "session": _identity(runtime), "where": event["content"]["where"],
                         "age_seconds": age, "stale": reason == "stale", "reason": reason,
                         "event_ids": sorted(e["event_id"] for e in heads)})
    priority = {"running": 4, "waiting": 3, "error": 2, "idle": 1, "unknown": 0}
    sessions.sort(key=lambda row: (priority[row["state"]], _time(row["observed_at"])), reverse=True)
    result = dict(sessions[0]) if sessions else {
        "state": "unknown", "observed_state": None, "observed_at": None,
        "event_id": None, "source": None, "session": None, "where": [],
        "age_seconds": None, "stale": False, "reason": "no_observation", "event_ids": [],
    }
    return result | {"fresh_seconds": fresh_seconds, "sessions": sessions,
                     "notice": "仅显示明确观测；过期、未来时间或冲突观测为未知。任务 active 不代表 Agent 正在运行。"}
