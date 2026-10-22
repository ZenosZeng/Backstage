"""Read existing workspace data without starting Agents or writing monitor state."""

from __future__ import annotations

from argparse import Namespace
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import threading
import time
import tomllib
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

from scripts import memory, retrieval
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
                    errors=errors, documents=documents, projects=projects, membership=membership, overview=overview)

    def overview(self):
        return self.snapshot()["overview"]

    def _knowledge_projects(self, events, documents, errors):
        """Use curated evidence, not a union of overlapping repository tags."""
        projects, membership, task_projects = {}, {}, {}
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
            if source.is_file():
                try:
                    curated = json.loads(source.read_text())
                    for raw in curated.get("events", []):
                        event = raw.get("event", raw)  # Both existing knowledge export formats.
                        if event.get("event_id"):
                            membership.setdefault(event["event_id"], set()).add(name)
                        if event.get("task_id") and event["task_id"] != "-":
                            task_projects.setdefault(event["task_id"], set()).add(name)
                except (ValueError, TypeError, AttributeError) as error:
                    errors.append(f"项目来源格式无效：{source.relative_to(self.root)}：{type(error).__name__}")
        for event in events:
            key = event["event_id"]
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

    def handoff(self, task, limit=5):
        snap = self.snapshot()
        result = memory.handoff(self.root, snap["events"], task, limit=min(20, max(1, int(limit))),
                                sources=snap["sources"])
        return result | {"errors": snap["errors"]}

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
        if path.suffix == ".svg":
            return {"path": relative, "kind": "svg", "asset": "api/asset?path=" + quote(relative)}
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
                "updated_at": timestamp(path.stat().st_mtime)}

    def services(self):
        items = []
        from scripts.doctor import code_identity
        installed = code_identity(service.ROOT)
        for name in service.SERVICES:
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

    def monitor(self, kind: str):
        if kind not in {"train", "b1k"}:
            raise ValueError("未知监控类型")
        config = self.local / "config" / f"watchdog-{kind}.json"
        data = {"configured": config.is_file(), "scope": "local", "source": str(config),
                "collector": f"watchdog-{kind} --once --no-notify"}
        if not data["configured"]:
            data.update(state="unconfigured", status="本机尚未配置此监控")
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
                    data.update(state="available" if payload["status"] else "no_data", status=payload["status"])
            except subprocess.TimeoutExpired:
                data.update(state="unavailable", error="监控读取超时，请检查采集环境后刷新")
        data.update(sampled_at=timestamp(datetime.now().timestamp()))
        data["read_at"] = data["sampled_at"]
        return data
