"""Per-chat native sessions, v1 migration and private atomic persistence."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import uuid

from tools.claude_feishu import harness


def normalize_chat(entry: dict) -> dict:
    legacy_keys = {
        "session_id",
        "created_at",
        "last_used",
        "cost_usd",
        "turns_total",
        "seconds_total",
        "ctx_used",
    }
    entry.setdefault("harness", "claude")
    buckets = entry.setdefault("sessions", {})
    if not isinstance(buckets, dict):
        raise ValueError("sessions must be a dict")
    if legacy_keys.intersection(entry):
        bucket = buckets.setdefault("claude", {})
        for key in legacy_keys:
            if key in entry:
                bucket.setdefault(key, entry.pop(key))
    return entry


def load_sessions(path: str | pathlib.Path) -> dict[str, dict]:
    state_path = pathlib.Path(path)
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text())
        if data.get("version", 1) == 1:
            backup = state_path.with_name(state_path.name + ".v1.bak")
            try:
                with backup.open("x") as handle:
                    handle.write(state_path.read_text())
                os.chmod(backup, 0o600)
            except FileExistsError:
                pass
        chats = data.get("chats", {})
        if not isinstance(chats, dict):
            raise ValueError("chats must be a dict")
        for entry in chats.values():
            if not isinstance(entry, dict):
                raise ValueError("chat entry must be a dict")
            normalize_chat(entry)
        return chats
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "session state unreadable (%s); context will restart fresh", exc
        )
        return {}


class SessionStore:
    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.chats = load_sessions(self.path)
        self.lock = threading.RLock()

    def persist(self) -> None:
        """原子写会话状态。

        审计 P2：命令线程（/cd /new）与执行线程会同时保存，此前共用
        `.{name}.{pid}.tmp` 一个临时文件名，先 replace 的线程会让另一个抛
        FileNotFoundError。加锁串行化，并把线程号写进临时名避免互踩。
        """
        with self.lock:
            state_path = self.path
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = state_path.with_name(
                f".{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            payload = {"version": 2, "chats": self.chats}
            temp_path.write_text(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            )
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, state_path)

    def get_or_create(
        self, chat_id: str, harness_name: str = "claude", *, enabled: bool = True
    ) -> tuple[str | None, bool]:
        if not enabled or not chat_id:
            return None, False
        with self.lock:
            bucket = self.bucket(chat_id, harness_name, create=True)
            if bucket.get("session_id"):
                return str(bucket["session_id"]), False
            if harness.REGISTRY[harness_name].session_mode == "capture":
                return None, False  # DSH can only resume an existing ID.
            sid = str(uuid.uuid4())
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            bucket.update(
                session_id=sid, created_at=bucket.get("created_at", now), last_used=now
            )
            self.persist()
            return sid, True

    def entry(self, chat_id: str, *, create: bool = False) -> dict:
        """该 chat 的持久化条目（会话 id / 工作目录 / 累计统计）。"""
        with self.lock:
            entry = self.chats.get(chat_id)
            if entry is None:
                if not create or not chat_id:
                    return {}
                entry = {}
                self.chats[chat_id] = entry
            return normalize_chat(entry)

    def harness_name(self, chat_id: str) -> str:
        with self.lock:
            name = self.entry(chat_id).get("harness", "claude")
            return name if name in harness.REGISTRY else "claude"

    def bucket(self, chat_id: str, name: str, *, create=False) -> dict:
        entry = self.entry(chat_id, create=create)
        buckets = entry.get("sessions", {})
        return (
            buckets.setdefault(name, {}) if create and entry else buckets.get(name, {})
        )

    def capture(
        self,
        chat_id: str,
        name: str,
        session_id: str,
        workdir: str,
        epoch: int = 0,
        *,
        enabled: bool = True,
    ) -> None:
        if not enabled or not chat_id:
            return
        with self.lock:
            bucket = self.bucket(chat_id, name, create=True)
            if bucket.get("epoch", 0) != epoch:
                return
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            bucket.update(
                session_id=session_id,
                cwd=workdir,
                created_at=bucket.get("created_at", now),
                last_used=now,
            )
            self.persist()

    def workdir(self, chat_id: str, default: str = "") -> str:
        """该 chat 的工作目录；/cd 未设过则用默认 default。"""
        entry = self.chats.get(chat_id) or {}
        workdir = str(entry.get("workdir") or "").strip()
        return workdir or default

    def update_stats(self, chat_id: str, state, elapsed: float) -> None:
        """把本轮的成本/上下文/轮次累加进会话条目，供 /cost /status 查询。"""
        if not chat_id:
            return
        with self.lock:
            entry = self.bucket(chat_id, state.harness, create=True)
            if state.cost is not None:
                entry["cost_usd"] = round(
                    float(entry.get("cost_usd") or 0.0) + state.cost, 6
                )
            entry["turns_total"] = int(entry.get("turns_total") or 0) + state.turns
            entry["seconds_total"] = round(
                float(entry.get("seconds_total") or 0.0) + elapsed, 1
            )
            for key, value in state.total_usage.items():
                if isinstance(value, (int, float)):
                    totals = entry.setdefault("total_usage", {})
                    totals[key] = totals.get(key, 0) + value
            used = state.context_used()
            if used:
                entry["ctx_used"] = used
            self.persist()

    def reset(self, chat_id: str, names) -> None:
        """Invalidate IDs while keeping workdir and cumulative usage."""
        with self.lock:
            for name in names:
                entry = self.bucket(chat_id, name, create=True)
                entry.pop("session_id", None)
                entry.pop("cwd", None)
                entry["epoch"] = int(entry.get("epoch", 0)) + 1
            self.persist()
