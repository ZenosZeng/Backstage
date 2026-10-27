"""Read existing workspace data without starting Agents or writing monitor state."""

from __future__ import annotations

from argparse import Namespace
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import threading
import time
import tomllib
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

from scripts import knowledge, memory, memory_review, retrieval, task_context
from tools import service
from tools.config import ROOT


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def summary(event: dict, sources: dict, replaced: set) -> dict:
    return {
        key: event.get(key) for key in (
            "event_id", "created_at", "machine_id", "agent", "title", "event_type",
            "status", "task_id", "topic_key",
        )
    } | {
        "projects": [p["name"] for p in event["projects"]],
        "excerpt": memory.short_text(event["content"].get("what", ""), 240),
        "source": sources[event["event_id"]],
        "replaced": event["event_id"] in replaced or event["status"] == "superseded",
    }


class Workspace:
    def __init__(self, root: Path, local: Path, *, ttl=3.0, clock=time.monotonic):
        self.root = root.resolve()
        self.local = local.resolve()
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._cached = None
        self._expires_at = 0

    def snapshot(self):
        # Share one generation across overview, memory and document queries.
        # A lock makes concurrent expiry rebuild once rather than once per caller.
        with self._lock:
            if self._cached is None or self.clock() >= self._expires_at:
                self._cached = self._load_snapshot()
                self._expires_at = self.clock() + self.ttl
            return self._cached

    def _load_snapshot(self):
        config = memory.load_config(self.root)
        sources = {}
        events, errors = memory.load_events(self.root, sources=sources)
        try:
            tasks = memory.task_states(events)
        except ValueError as error:
            errors.append(str(error))
            tasks = {}
        replaced = {key for event in events for key in event.get("supersedes", [])}
        documents = self._scan_documents()
        freshness = knowledge.freshness_index(self.root, documents, events, config)
        for document in documents:
            document["freshness"] = freshness.get(document["path"], {})
        projects, membership = self._knowledge_projects(events, documents, errors)
        repositories = {}
        for name, item in config.get("projects", {}).items():
            repositories[name] = {"name": name, "path": item.get("path", ""), "events": 0,
                                  "active_tasks": 0, "kind": "repository",
                                  "last_event_at": None, "latest_title": ""}
        machines = {}
        activity = Counter()
        task_associations = {}
        for event in events:
            machines[event["machine_id"]] = event["created_at"]
            activity[event["created_at"][:10]] += 1
            if event.get("task_id"):
                task_associations.setdefault(event["task_id"], set()).update(membership.get(event["event_id"], []))
            for project in event["projects"]:
                item = repositories.setdefault(project["name"], {"name": project["name"], "path": "",
                                               "events": 0, "active_tasks": 0, "kind": "repository"})
                item.update(events=item["events"] + 1, last_event_at=event["created_at"],
                            latest_title=event["title"])
        task_list = []
        for task in tasks.values():
            associated = sorted(task_associations.get(task["task_id"], []))
            task_list.append({"task_id": task["task_id"], "state": task["state"],
                              "projects": task["projects"], "business_projects": associated})
            if task["state"] == "active":
                for name in task["projects"]:
                    repositories[name]["active_tasks"] += 1
                for name in associated:
                    projects[name]["active_tasks"] += 1
        base = self.local / ".sync/base"
        version_file = self.root / "pixi.toml"
        version = tomllib.loads(version_file.read_text())["workspace"]["version"] if version_file.exists() else ""
        now = timestamp(datetime.now().timestamp())
        current = [e for e in events if e["event_id"] not in replaced and e["status"] != "superseded"]
        attention = [e for e in current if e["status"] in {"active", "needs_verification"}
                     and e["event_type"] in {"risk", "hypothesis"}
                     and tasks.get(e.get("task_id"), {}).get("state") not in {"paused", "closed"}]
        overview = {
            "workspace": config["workspace"], "machine_id": config["machine_id"],
            "version": version, "sampled_at": now, "read_at": now, "cache_seconds": self.ttl,
            "latest_event_at": events[-1]["created_at"] if events else None,
            "shared_base_at": timestamp(base.stat().st_mtime) if base.exists() else None,
            "counts": {"events": len(events), "projects": len(projects), "repositories": len(repositories),
                       "active_tasks": sum(t["state"] == "active" for t in task_list),
                       "attention": len(attention), "documents": len(documents)},
            "project_mode": "knowledge" if projects else "repositories",
            "projects": sorted((projects or repositories).values(), key=lambda p: p.get("last_event_at") or "", reverse=True),
            "repositories": sorted(repositories.values(), key=lambda p: p["name"]),
            "tasks": sorted(task_list, key=lambda t: (t["state"] != "active", t["task_id"])),
            "machines": [{"name": name, "last_event_at": at} for name, at in sorted(machines.items())],
            "agents": sorted({e["agent"] for e in events}),
            "activity": [{"date": date, "count": count} for date, count in sorted(activity.items())[-30:]],
            "recent": [summary(e, sources, replaced) for e in reversed(current[-8:])],
            "attention": [summary(e, sources, replaced) for e in reversed(attention[-5:])],
            "errors": errors,
        }
        return dict(config=config, events=events, sources=sources, tasks=tasks, replaced=replaced,
                    freshness=freshness,
                    errors=errors, documents=documents, projects=projects, membership=membership, overview=overview)

    def overview(self):
        return self.snapshot()["overview"]

    def timeline(self, project=None, task=None):
        """Project recorded activity; dates are evidence bounds, never a plan."""
        snap = self.snapshot()
        if task and not project:
            raise ValueError("筛选任务时须同时指定项目")
        available = {item["name"]: item for item in snap["overview"]["projects"]}
        if project and project not in available:
            raise FileNotFoundError("项目不存在")
        selected = {project: available[project]} if project else available
        grouped = {name: [] for name in selected}
        current_by_task = {}
        knowledge = bool(snap["projects"])
        for event in snap["events"]:
            if event["event_id"] not in snap["replaced"] and event["status"] != "superseded":
                current_by_task.setdefault(event.get("task_id"), []).append(event)
            names = snap["membership"].get(event["event_id"], ()) if knowledge else (
                item["name"] for item in event["projects"]
            )
            for name in names:
                if name in grouped:
                    grouped[name].append(event)

        def at(event):
            return memory.parse_time(event["created_at"]).isoformat()

        def bounds(events):
            return (at(events[0]), at(events[-1])) if events else (None, None)

        projects, project_tasks = [], {}
        for name, metadata in selected.items():
            events = grouped[name]  # The shared snapshot is already sorted in UTC.
            by_task = {}
            for event in events:
                if event.get("task_id") not in (None, "", "-"):
                    by_task.setdefault(event["task_id"], []).append(event)
            project_tasks[name] = set(by_task)
            rows = []
            counts = dict.fromkeys(("active", "paused", "closed", "unknown", "conflict"), 0)
            for task_id, related in by_task.items():
                lifecycle = snap["tasks"].get(task_id, {})
                state = lifecycle.get("state", "unknown")
                counts[state] += 1
                first, last = bounds(related)
                title = next((e["title"] for e in related if "task_state" not in e["content"]), task_id)
                rows.append({
                    "task_id": task_id, "title": memory.short_text(title, 100), "state": state,
                    "first_recorded_at": first, "last_recorded_at": last,
                    "last_title": related[-1]["title"],
                    "state_heads": [{"event_id": e["event_id"], "created_at": at(e),
                                     "task_state": e["content"]["task_state"]}
                                    for e in lifecycle.get("heads", [])],
                    "execution": task_context.execution_status(current_by_task.get(task_id, []), task_id,
                                                               sources=snap["sources"]) if project else None,
                    "session_count": len(task_context.session_references(current_by_task.get(task_id, []), task_id,
                                                                        sources=snap["sources"])) if project else None,
                })
            if task and task not in by_task:
                raise FileNotFoundError("任务不属于此项目")
            first, last = bounds(events)
            item = {
                "name": name, "title": metadata.get("title", name),
                "description": metadata.get("description", metadata.get("path", "")),
                "kind": metadata["kind"], "home": metadata.get("home"),
                "freshness": snap["freshness"].get(metadata.get("home"), {}),
                "documents": metadata.get("documents", []),
                "repositories": metadata.get("repositories", [name]),
                "first_recorded_at": first, "last_recorded_at": last,
                "event_count": len(events), "task_count": len(rows), "task_counts": counts,
            }
            if project:
                item["tasks"] = rows
                related = by_task[task] if task else events
                # Keep lifecycle history even when a later transition supersedes
                # it. Ordinary obsolete conclusions do not become milestones.
                current = [e for e in related if "task_state" in e["content"] or (
                    e["event_id"] not in snap["replaced"] and e["status"] != "superseded"
                )]
                candidates = {e["event_id"] for e in current
                              if "task_state" in e["content"] or
                              e["event_type"] in {"decision", "bugfix", "test_result", "risk"}}
                if current:
                    candidates.update((current[0]["event_id"], current[-1]["event_id"]))
                milestones = [e for e in current if e["event_id"] in candidates]
                total = len(milestones)
                if total > 40:
                    # Even sampling preserves both ends of the project's history.
                    milestones = [milestones[round(i * (total - 1) / 39)] for i in range(40)]
                item["milestones"] = [summary(e, snap["sources"], snap["replaced"]) | {
                    "created_at": at(e), "task_state": e["content"].get("task_state"),
                    "is_historical": e["event_id"] in snap["replaced"] or e["status"] == "superseded",
                } for e in milestones]
                item.update(milestone_total=total, milestone_omitted=total - len(milestones))
            projects.append(item)
        projects.sort(key=lambda item: (item["last_recorded_at"] or "", item["name"]), reverse=True)
        starts = [p["first_recorded_at"] for p in projects if p["first_recorded_at"]]
        ends = [p["last_recorded_at"] for p in projects if p["last_recorded_at"]]
        # Compute deduplicated counts and the range from this same generation.
        # Shared tasks may appear in several project rows, but count only once.
        selected_ids = {e["event_id"] for related in grouped.values() for e in related}
        task_ids = {e["task_id"] for related in grouped.values() for e in related
                    if e.get("task_id") not in (None, "", "-")}
        counts = Counter(snap["tasks"].get(key, {}).get("state", "unknown") for key in task_ids)
        all_ids = {e["event_id"] for e in snap["events"]}
        all_tasks = {e["task_id"] for e in snap["events"] if e.get("task_id") not in (None, "", "-")}
        assigned_tasks = {e["task_id"] for e in snap["events"]
                          if e.get("task_id") not in (None, "", "-") and snap["membership"].get(e["event_id"])}
        attention = []
        for task_id in sorted(task_ids):
            lifecycle = snap["tasks"].get(task_id, {})
            related = current_by_task.get(task_id, [])
            risks = [e for e in related if e["event_id"] in selected_ids
                     and e["event_id"] not in snap["replaced"] and e["status"] in {"active", "needs_verification"}
                     and e["event_type"] in {"risk", "hypothesis"}]
            execution = task_context.execution_status(related, task_id, sources=snap["sources"])
            reasons = []
            if lifecycle.get("state") == "conflict":
                reasons.append("业务状态有冲突")
            session_states = {item["state"] for item in execution["sessions"]}
            if "waiting" in session_states:
                reasons.append("Agent 等待输入")
            if "error" in session_states:
                reasons.append("Agent 报告错误")
            if risks and lifecycle.get("state") not in {"paused", "closed"}:
                reasons.append(f"{len(risks)} 项未决风险 / 待验证结论")
            if reasons:
                attention.append({"task_id": task_id, "state": lifecycle.get("state", "unknown"),
                                  "reasons": reasons, "execution": execution,
                                  "event_ids": [e["event_id"] for e in risks[-5:]],
                                  "projects": [name for name in selected if task_id in project_tasks[name]]})
        return {"projects": projects, "range": {"start": min(starts, default=None),
                                                 "end": max(ends, default=None)},
                "task_counts": dict.fromkeys(("active", "paused", "closed", "unknown", "conflict"), 0) | dict(counts),
                "task_count": len(task_ids),
                "coverage": {"total_events": len(all_ids), "associated_events": len(selected_ids),
                             "unassigned_events": len(all_ids - {key for key, names in snap["membership"].items() if names})
                             if knowledge else 0,
                             "unassigned_tasks": len(all_tasks - assigned_tasks) if knowledge else 0,
                             "unassigned_task_items": [{"task_id": key,
                                 "state": snap["tasks"].get(key, {}).get("state", "unknown")}
                                 for key in sorted(all_tasks - assigned_tasks)[:20]] if knowledge else []},
                "attention": attention[:8], "attention_count": len(attention),
                "task_detail": self._task_detail(snap, task) if task else None,
                "sampled_at": snap["overview"]["sampled_at"],
                "project_mode": snap["overview"]["project_mode"], "errors": snap["errors"]}

    @staticmethod
    def _task_detail(snap, task):
        # Membership was checked above; a stable task can span business projects.
        # Its current conclusions share the same scope as sessions and handoff.
        related = [e for e in snap["events"] if e.get("task_id") == task
                   and e["event_id"] not in snap["replaced"]
                   and e["status"] != "superseded" and "task_state" not in e["content"]]
        def entries(kinds, attention=False):
            selected = [e for e in related if e["event_type"] in kinds
                        and (not attention or e["status"] in {"active", "needs_verification"})]
            return [summary(e, snap["sources"], snap["replaced"]) for e in reversed(selected[-5:])]
        return {"task_id": task, "sessions": task_context.session_references(
                    snap["events"], task, sources=snap["sources"]),
                "execution": task_context.execution_status(snap["events"], task, sources=snap["sources"]),
                "decisions": entries({"decision", "preference", "config"}),
                "results": entries({"test_result", "bugfix"}),
                "attention": entries({"risk", "hypothesis"}, attention=True)}

    def _knowledge_projects(self, events, documents, errors):
        """Use curated evidence, not a union of overlapping repository tags."""
        projects, membership, task_projects = {}, {}, {}
        replaced = {key for event in events for key in event.get("supersedes", [])}
        event_index = {event["event_id"]: event for event in events}
        directory = self.root / ".share/knowledge/projects"
        available = {d["path"] for d in documents}
        for home in sorted(directory.glob("*.md")):
            if home.name == "README.md":
                continue
            relative = home.relative_to(self.root).as_posix()
            if relative not in available:
                continue
            text = home.read_text()
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), home.stem)
            description = next((p.strip().replace("\n", " ") for p in text.split("\n\n")
                                if p.strip() and not p.startswith(("#", "![", "更新"))), "")
            name = home.stem
            docs = {relative}
            diagram = home.with_suffix(".svg").relative_to(self.root).as_posix()
            if diagram in available:
                docs.add(diagram)
            docs.update(path for path in available if path.startswith(f".share/docs/{name}/"))
            for block in MarkdownIt("js-default").parse(text):
                for token in block.children or []:
                    link = token.attrGet("href") or token.attrGet("src")
                    if not link:
                        continue
                    url = urlsplit(link)
                    if not url.scheme and not url.netloc and url.path:
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(url.path)))
                        if target in available:
                            docs.add(target)
            projects[name] = {"name": name, "title": title, "description": description, "kind": "knowledge",
                              "home": relative, "documents": sorted(docs), "repositories": [],
                              "events": 0, "active_tasks": 0, "last_event_at": None, "latest_title": ""}
            source = home.with_suffix(".json")
            if source.is_file() and source.resolve().is_relative_to(self.root):
                try:
                    curated = json.loads(source.read_text())
                    for raw in curated.get("events", []):
                        event = raw.get("event", raw)  # Both existing knowledge export formats.
                        if event.get("event_id"):
                            membership.setdefault(event["event_id"], set()).add(name)
                        # Keep historical membership, but never inherit a task
                        # assignment from an explicitly obsolete source snapshot.
                        current = event_index.get(event.get("event_id"), event)
                        if (current.get("task_id") not in (None, "", "-")
                                and current.get("event_id") not in replaced
                                and current.get("status") != "superseded"
                                and "business_projects" not in current.get("content", {})):
                            # Explicit raw assignments are handled below, even
                            # when this document cites them as background evidence.
                            task_projects.setdefault(current["task_id"], set()).add(name)
                except (ValueError, TypeError, AttributeError) as error:
                    errors.append(f"项目来源格式无效：{source.relative_to(self.root)}：{type(error).__name__}")
        for event in events:
            explicit = event["content"].get("business_projects")
            if (explicit is not None and event.get("task_id") not in (None, "", "-")
                    and event["event_id"] not in replaced and event["status"] != "superseded"):
                task_projects.setdefault(event["task_id"], set()).update(set(explicit) & projects.keys())
        for event in events:
            key = event["event_id"]
            explicit = event["content"].get("business_projects")
            if explicit is not None:
                membership[key] = set(explicit) & projects.keys()
                missing = set(explicit) - projects.keys()
                if missing:
                    errors.append(f"{key} 引用未整理的业务项目：{', '.join(sorted(missing))}")
            if key not in membership:
                # Continue unambiguous curated tasks; shared workday tasks do not
                # make all future records belong to every historical project.
                candidates = task_projects.get(event.get("task_id"), set())
                if len(candidates) == 1:
                    membership[key] = set(candidates)
                else:
                    namespace = (str(event.get("task_id") or ""), event["topic_key"])
                    membership[key] = {name for name in projects if any(
                        value == name or value.startswith((name + "/", name + "-")) for value in namespace
                    )}
            for name in membership[key]:
                project = projects[name]
                project.update(events=project["events"] + 1, last_event_at=event["created_at"], latest_title=event["title"])
                project["repositories"] = sorted(set(project["repositories"]) | {p["name"] for p in event["projects"]})
        return projects, membership

    def events(self, query: dict):
        snap = self.snapshot()
        events, sources, replaced, errors = (snap[k] for k in ("events", "sources", "replaced", "errors"))
        args = Namespace(
            query=query.get("q", ""), project=query.get("project"), task=query.get("task"),
            machine=query.get("machine"), agent=query.get("agent"), topic=None,
            current=query.get("current", "true") == "true", limit=None,
            sort=query.get("sort") or ("relevance" if query.get("q") else "recent"),
            aliases=snap["config"].get("search_aliases", {}),
            since=memory.filter_time(query["since"]) if query.get("since") else None,
            until=memory.filter_time(query["until"]) if query.get("until") else None,
        )
        if args.sort not in {"relevance", "recent"}:
            raise ValueError("未知排序")
        selected = memory.select_events(events, args)
        if query.get("business"):
            selected = [e for e in selected if query["business"] in snap["membership"].get(e["event_id"], [])]
        if query.get("type"):
            selected = [e for e in selected if e["event_type"] == query["type"]]
        offset = max(0, int(query.get("offset", 0)))
        limit = min(100, max(1, int(query.get("limit", 30))))
        return {"total": len(selected), "offset": offset,
                "items": [summary(e, sources, replaced) for e in selected[offset:offset + limit]],
                "errors": errors}

    def event(self, event_id: str):
        snap = self.snapshot()
        events, sources, replaced, errors = (snap[k] for k in ("events", "sources", "replaced", "errors"))
        for event in events:
            if event["event_id"] == event_id:
                return {"event": event, "source": sources[event_id],
                        "replaced": event_id in replaced or event["status"] == "superseded",
                        "replaced_by": [e["event_id"] for e in events if event_id in e.get("supersedes", [])],
                        "errors": errors}
        raise FileNotFoundError("记忆不存在")

    def handoff(self, task, limit=5, to_agent=None):
        snap = self.snapshot()
        result = memory.handoff(self.root, snap["events"], task, limit=min(20, max(1, int(limit))),
                                sources=snap["sources"], to_agent=to_agent)
        return result | {"errors": snap["errors"]}

    def review(self, task, limit=20, max_chars=12000):
        snap = self.snapshot()
        if task not in snap["tasks"]:
            raise FileNotFoundError("任务不存在")
        limit, max_chars = int(limit), int(max_chars)
        if not 1 <= limit <= 50 or not 1000 <= max_chars <= 30000:
            raise ValueError("审查条数须为 1–50，字符预算须为 1000–30000")
        return memory_review.prepare_review(snap["events"], snap["sources"], task=task,
                                            limit=limit, max_chars=max_chars)

    def context(self, event_id, before=3, after=3):
        snap = self.snapshot()
        return memory.event_context(snap["events"], snap["sources"], event_id,
                                    before=int(before), after=int(after)) | {"errors": snap["errors"]}

    def doctor(self):
        from scripts.doctor import inspect_workspace
        return inspect_workspace(self.root, local=self.local)

    def document_path(self, value: str) -> Path:
        return retrieval.document_path(self.root, value)

    def documents(self, business=None, query='', sort='relevance'):
        snap = self.snapshot()
        documents = snap["documents"]
        if business:
            project = snap["projects"].get(business)
            if project is None:
                raise FileNotFoundError("业务项目不存在")
            documents = [d for d in documents if d["path"] in project["documents"]]
        return retrieval.search_documents(documents, query, aliases=snap["config"].get("search_aliases", {}), sort=sort)

    def _scan_documents(self):
        return retrieval.scan_documents(self.root)

    def document(self, value: str):
        path = self.document_path(value)
        relative = path.relative_to(self.root).as_posix()
        # Previewing one document must not freeze the entire directory listing.
        # Normal navigation already has a snapshot; standalone previews can
        # check this document without creating a second directory cache.
        snap = self._cached if self.clock() < self._expires_at else None
        freshness = snap["freshness"].get(relative) if snap else None
        if freshness is None:
            config = snap["config"] if snap else memory.load_config(self.root)
            events = snap["events"] if snap else memory.load_events(self.root)[0]
            freshness = knowledge.document_freshness(self.root, relative, events, config)
        if path.suffix == ".svg":
            return {"path": relative, "kind": "svg", "asset": "api/asset?path=" + quote(relative),
                    "freshness": freshness}
        if path.suffix != ".md":
            raise ValueError("此入口仅预览 Markdown / SVG")
        parser = MarkdownIt("js-default").enable("table")
        tokens = parser.parse(path.read_text())
        for block in tokens:
            for token in block.children or []:
                attr = "src" if token.type == "image" else "href" if token.type == "link_open" else None
                if not attr:
                    continue
                url = urlsplit(token.attrGet(attr) or "")
                if url.scheme or url.netloc:
                    if token.type == "link_open":
                        token.attrSet("target", "_blank")
                        token.attrSet("rel", "noopener noreferrer")
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(url.path))) if url.path else relative
                if token.type == "image":
                    token.attrSet(attr, "api/asset?path=" + quote(target))
                else:
                    token.attrSet(attr, "#/documents?path=" + quote(target, safe="") +
                                  ("&anchor=" + quote(url.fragment) if url.fragment else ""))
        return {"path": relative, "kind": "md", "html": parser.renderer.render(tokens, parser.options, {}),
                "updated_at": timestamp(path.stat().st_mtime), "freshness": freshness}

    def services(self):
        items = []
        from scripts.doctor import code_identity
        installed = code_identity(service.ROOT)
        for name in service.service_names(self.local):
            directory = self.local / "tools" / name
            record = service.running_record(directory, name)
            started = record.get("code_at_start") if record else None
            changed = None
            if isinstance(started, dict):
                comparable = [key for key in ("version", "commit") if started.get(key) and installed.get(key)]
                if any(started[key] != installed[key] for key in comparable):
                    changed = True
                elif len(comparable) == 2 and started.get("dirty") is False and installed.get("dirty") is False:
                    changed = False
            items.append({"name": name, "running": bool(record),
                          "ready": bool(record and record.get("ready")),
                          "pid": record["pid"] if record else None,
                          "code_at_start": started, "code_changed": changed,
                          "source": str(directory / "pid.json"),
                          "configured": (self.local / "config" / f"{name}.json").is_file()})
        return {"items": items, "installed_code": installed, "scope": "local", "sampled_at": timestamp(datetime.now().timestamp()),
                "read_at": timestamp(datetime.now().timestamp())}

    def monitors(self):
        """List local adapter names/configuration without importing project code."""
        from tools.watchdog.loader import available_adapters

        available = set(available_adapters())
        configured = {path.name[len("watchdog-"):-len(".json")]
                      for path in (self.local / "config").glob("watchdog-*.json")
                      if path.is_file()}
        names = sorted(name for name in available | configured
                       if re.fullmatch(r"[a-z][a-z0-9_]*", name))
        items = []
        for name in names:
            adapter = name
            if name in configured:
                try:
                    config = json.loads((self.local / "config" / f"watchdog-{name}.json").read_text())
                    adapter = config.get("adapter", name) if isinstance(config, dict) else None
                except (OSError, ValueError):
                    adapter = None
            if not isinstance(adapter, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", adapter):
                adapter = None
            items.append({"name": name, "adapter": adapter, "available": adapter in available,
                          "configured": name in configured})
        return {"items": items, "scope": "local"}

    def monitor(self, kind: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
            raise ValueError("未知监控类型")
        item = next((item for item in self.monitors()["items"] if item["name"] == kind), None)
        if item is None:
            raise ValueError("未知监控类型")
        config = self.local / "config" / f"watchdog-{kind}.json"
        data = {"configured": config.is_file(), "available": item["available"],
                "scope": "local", "source": str(config),
                "collector": f"watchdog-{kind} --once --no-notify"}
        if not data["configured"]:
            data.update(state="unconfigured", status="本机尚未配置此监控")
        elif not item["available"]:
            data.update(state="unavailable", error="本机未安装此监控的 adapter")
        else:
            # Reuse the read-only CLI in isolation; never send or save cursors.
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "tools.watchdog", kind, "--once", "--no-notify", "--config", str(config)],
                    cwd=ROOT, env={**os.environ, "AGENTS_LOCAL_ROOT": str(self.local)},
                    capture_output=True, text=True, timeout=45,
                )
                if result.returncode:
                    data.update(state="unavailable", error="监控读取失败，请检查本机监控配置与日志")
                else:
                    payload = json.loads(result.stdout)
                    status = payload["status"]
                    if not isinstance(status, str):
                        raise ValueError("invalid monitor status")
                    data.update(state="available" if status else "no_data", status=status)
            except subprocess.TimeoutExpired:
                data.update(state="unavailable", error="监控读取超时，请检查采集环境后刷新")
            except (OSError, ValueError, KeyError, TypeError):
                data.update(state="unavailable", error="监控读取失败，请检查本机监控配置与日志")
        data.update(sampled_at=timestamp(datetime.now().timestamp()))
        data["read_at"] = data["sampled_at"]
        return data

    def cluster(self):
        """Read the collector's local snapshot; HTTP requests never start probes."""
        from tools.cluster.config import read_snapshot, settings_fingerprint

        directory = self.local / "tools" / "cluster-monitor"
        config = self.local / "config" / "cluster-monitor.json"
        now = time.time()
        data = {
            "configured": config.is_file(), "scope": "cluster",
            "source": str(directory / "snapshot.json"),
            "read_at": timestamp(now), "sampled_at": None,
            "collector_running": False, "snapshot": None,
        }
        if not data["configured"]:
            return data | {"state": "unconfigured", "status": "本机尚未配置集群监控"}
        try:
            fingerprint = settings_fingerprint(json.loads(config.read_text()))
        except (OSError, ValueError, TypeError):
            return data | {"state": "unavailable", "error": "集群监控配置无效或不可读"}
        record = service.running_record(directory, "cluster-monitor")
        data["collector_running"] = bool(record)
        try:
            snapshot = read_snapshot(directory)
        except FileNotFoundError:
            return data | {
                "state": "no_data" if record else "unavailable",
                "status": "采集器正在等待首轮快照" if record else "采集器未运行，暂无集群快照",
            }
        except (OSError, ValueError):
            return data | {"state": "unavailable", "error": "集群快照无效或不可读，请检查采集器日志"}
        if snapshot["config_hash"] != fingerprint:
            return data | {"state": "no_data", "status": "配置已变化，需重启集群采集器才能应用新配置"}
        age = now - snapshot["time"]
        fresh = (-5 <= age <= snapshot["interval"] * 3 + 5
                 and bool(record) and snapshot["state"] == "collecting")
        has_sample = any(node.get("sample") is not None for node in snapshot["nodes"])
        if not fresh:
            for node in snapshot["nodes"]:
                if node.get("sample") is not None:
                    node["status"] = "stale"
            if snapshot.get("ports"):
                snapshot["ports"]["traffic_stale"] = True
        data.update(snapshot=snapshot, sampled_at=timestamp(snapshot["time"]))
        if not has_sample:
            return data | {"state": "no_data", "status": "尚未收到节点数据，请查看节点连接状态"}
        if not fresh or not any(node.get("status") == "online" for node in snapshot["nodes"]):
            return data | {
                "state": "stale",
                "status": "采集器未运行，显示最后快照" if not record else "采集数据已过期，显示最后读数",
            }
        return data | {"state": "available", "status": "集群资源只读采样"}
