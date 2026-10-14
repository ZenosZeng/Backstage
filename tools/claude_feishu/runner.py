"""Run headless Agents and own their process groups; no Feishu transport."""

from __future__ import annotations

from dataclasses import replace
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Callable

from tools.claude_feishu import harness
from tools.claude_feishu.sessions import SessionStore
from tools.claude_feishu.presentation import RunState

_ENV_ALLOW = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "KUBECONFIG",
    "CONDA_OVERRIDE_CUDA",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SSE_PORT",
}
_ENV_ALLOW_PREFIXES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def build_env(harness_name: str = "claude") -> dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        if key in _ENV_ALLOW | harness.REGISTRY[harness_name].env_allow() or any(
            key.startswith(p) for p in _ENV_ALLOW_PREFIXES
        ):
            env[key] = value
    if harness_name == "dsh":
        mode = os.environ.get("CLAUDE_FEISHU_DSH_PERMISSION_MODE", "read-only")
        if mode not in {"read-only", "workspace-write"}:
            raise ValueError("invalid DSH permission mode")
        env["DSH_PERMISSION_MODE"] = mode
    return env


def kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 - 进程可能已退出
        pass


class AgentRunner:
    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions
        self.process_lock = threading.Lock()
        self.stopping = threading.Event()
        self.interrupt_requested = threading.Event()
        self.active_process = None

    def run(
        self,
        prompt: str,
        chat_id: str = "",
        on_progress: Callable[[RunState, float], None] | None = None,
        *,
        harness_name: str = "claude",
        claude_cli: str,
        settings: str,
        default_workdir: str,
        session_context_enabled: bool,
        timeout: float,
        progress_interval: float,
        state_factory: Callable[[str], RunState],
    ) -> tuple[str, RunState]:
        """Execute the prompt headless in a fresh process group; returns (回复, 状态)。

        会话上下文：同一 chat 复用 claude 原生会话（新建用 --session-id 固定 id，
        之后 -r 恢复），对话历史（含工具调用）由 claude 自己持久化并自动压缩。
        会话被外部清理（~/.claude/projects 被删）时自动重建一次。

        进度：用 --output-format stream-json 逐行读事件（而非 communicate 缓冲到
        结束），据此每 progress_interval 秒回调一次 on_progress(state, 已等待秒数)；
        最终回答取 result 事件，缺失时回退到累积文本。总时长受 timeout 参数限制。
        打断：进程组被外部终止时 stdout 直接 EOF，按中断返回。
        """
        self.interrupt_requested.clear()  # 只算本轮的打断请求
        workdir = self.sessions.workdir(chat_id, default_workdir)
        adapter = harness.REGISTRY[harness_name]
        if harness_name == "claude":
            adapter = replace(adapter, binary=claude_cli)
        if not adapter.available():
            failed = state_factory(harness_name)
            failed.returncode = 1
            return f"本机未安装 {harness_name}", failed
        epoch = self.sessions.bucket(chat_id, harness_name, create=True).get("epoch", 0)
        retried = False
        while not self.stopping.is_set():
            session_id, is_new = self.sessions.get_or_create(
                chat_id, harness_name, enabled=session_context_enabled
            )
            cmd = adapter.build_argv(
                prompt,
                session_id=session_id,
                is_new=is_new,
                workdir=workdir,
                settings=settings,
            )
            state = state_factory(harness_name)
            state.workdir = workdir
            with self.process_lock:
                if self.stopping.is_set():
                    return "Service is stopping", state_factory(harness_name)
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=build_env(harness_name),
                    stdin=subprocess.PIPE if harness_name == "dsh" else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self.active_process = proc
            try:
                started = time.monotonic()
                deadline = started + timeout

                # stderr 单独线程抽干，避免子进程写满 stderr 管道后阻塞
                err_buf: list[str] = []

                def _drain_stderr() -> None:
                    try:
                        for line in proc.stderr:  # type: ignore[union-attr]
                            err_buf.append(line)
                    except Exception:  # noqa: BLE001 - 管道关闭即结束
                        pass

                stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
                stderr_thread.start()

                # 读线程 + 队列：超时与进度回调不能依赖"有输出"——claude 挂住不产出时
                # 直接 for line in proc.stdout 会永久阻塞，EXEC_TIMEOUT 永远不触发。
                lines_q: queue.Queue = queue.Queue()

                def _pump_stdout() -> None:
                    try:
                        for raw in proc.stdout:  # type: ignore[union-attr]
                            lines_q.put(raw)
                    except Exception:  # noqa: BLE001 - 管道关闭即结束
                        pass
                    finally:
                        lines_q.put(None)

                threading.Thread(target=_pump_stdout, daemon=True).start()
                if harness_name == "dsh":

                    def _send_task(task_proc=proc):
                        try:
                            task_proc.stdin.write(prompt)
                            task_proc.stdin.close()
                        except (BrokenPipeError, OSError, ValueError):
                            pass

                    threading.Thread(target=_send_task, daemon=True).start()

                last_tick = started
                saw_event = False
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        kill_process_group(proc)
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    try:
                        line = lines_q.get(timeout=min(1.0, max(0.05, deadline - now)))
                    except queue.Empty:
                        if (
                            on_progress
                            and progress_interval > 0
                            and now - last_tick >= progress_interval
                        ):
                            last_tick = now
                            try:
                                on_progress(state, now - started)
                            except Exception:  # noqa: BLE001 - 提醒失败不中断执行
                                logging.warning("progress notify failed", exc_info=True)
                        continue
                    if line is None:  # stdout EOF
                        break
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("{"):
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                continue
                            captured = adapter.session_from_event(event)
                            if captured:
                                self.sessions.capture(
                                    chat_id,
                                    harness_name,
                                    captured,
                                    str(event.get("cwd") or workdir),
                                    epoch,
                                    enabled=session_context_enabled,
                                )
                            state.observe(event)
                            saw_event = True
                        except (ValueError, TypeError):
                            pass  # 非 JSON 或结构异常的行忽略，不影响最终结果
                    now = time.monotonic()
                    if (
                        on_progress
                        and progress_interval > 0
                        and now - last_tick >= progress_interval
                    ):
                        last_tick = now
                        try:
                            on_progress(state, now - started)
                        except Exception:  # noqa: BLE001 - 提醒失败不中断执行
                            logging.warning("progress notify failed", exc_info=True)
                try:
                    proc.wait(timeout=min(30, max(0.05, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    # stdout 已 EOF 但进程没退（审计 P1：这里曾直接抛出，进程组
                    # 没人清、self.active_process 又被置空，留下失控子进程还回"已终止"）
                    logging.error(
                        "%s 未随 stdout 关闭退出，终止进程组 pid=%s",
                        harness_name,
                        proc.pid,
                    )
                    kill_process_group(proc)
                    raise
                state.returncode = proc.returncode
                state.interrupt_requested = self.interrupt_requested.is_set()
                stderr_thread.join(timeout=1)
                err = "".join(err_buf).strip()
                logging.info(
                    "%s exited rc=%s events=%s turns=%s interrupted=%s",
                    harness_name,
                    proc.returncode,
                    saw_event,
                    state.turns,
                    state.interrupted,
                )
            finally:
                with self.process_lock:
                    if self.active_process is proc:
                        self.active_process = None
                # 兜底（审计 P1）：任何异常退出路径都不许留下活着的子进程组
                if proc.poll() is None:
                    logging.warning(
                        "异常路径下 %s 仍存活，终止进程组 pid=%s",
                        harness_name,
                        proc.pid,
                    )
                    kill_process_group(proc)
            out = state.answer().strip()
            if not out and err:
                out = err[-1500:]  # 无 result 事件时回落到 stderr 摘要
            if proc.returncode != 0:
                # 会话文件被外部删除（如手动清理 ~/.claude/projects）：摘除映射重建一次
                if (
                    not retried
                    and not is_new
                    and session_id
                    and self.sessions.bucket(chat_id, harness_name).get("epoch", 0)
                    == epoch
                    and adapter.recoverable(err or state.stream_error)
                ):
                    logging.warning(
                        "%s session unavailable; recreating (chat=%s)",
                        harness_name,
                        chat_id or "?",
                    )
                    with self.sessions.lock:
                        bucket = self.sessions.bucket(chat_id, harness_name)
                        if bucket.get("epoch", 0) != epoch:
                            return (out or "(无输出)"), state
                        bucket.pop("session_id", None)
                        self.sessions.persist()
                    retried = True
                    continue
                return (
                    f"⚠️ {harness_name} 执行失败 (rc={proc.returncode})\n```\n{(state.stream_error or err or state.terminal_reason or out)[:1500]}\n```",
                    state,
                )
            if session_id or state.session_id:
                with self.sessions.lock:
                    entry = self.sessions.bucket(chat_id, harness_name)
                    if entry and entry.get("epoch", 0) == epoch:
                        entry["last_used"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        self.sessions.persist()
            return (out or "(无输出)"), state
        return "Service is stopping", state_factory(harness_name)

    def interrupt(self) -> bool:
        """终止当前执行的进程组；没有在跑的进程返回 False。

        特意绕开 _lock：执行期间 _execute_and_reply 一直持锁，打断消息必须能
        直接够到 self.active_process，否则只能回一句"忙"。
        """
        with self.process_lock:
            proc = self.active_process
        if proc is None or proc.poll() is not None:
            return False
        # 先立标记再杀：CLI 会捕获 SIGTERM 并以 rc=143 正常退出，光看 returncode
        # 分不出"被我们打断"和"自己失败退出"。
        self.interrupt_requested.set()
        logging.info("interrupt requested; killing process group pid=%s", proc.pid)
        kill_process_group(proc)
        return True

    def shutdown(self) -> None:
        """Stop only the remote command group owned by this bridge."""
        self.stopping.set()
        with self.process_lock:
            proc = self.active_process
        if proc is not None and proc.poll() is None:
            kill_process_group(proc)
