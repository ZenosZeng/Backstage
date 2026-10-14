#!/usr/bin/env python3
"""TrainMonitor: event-driven training and offline-evaluation watchdog.

Emits four event kinds to Feishu as mobile-friendly markdown cards:
    start    — a new training or offline-evaluation job appeared
    finished — job succeeded (or progress reached 100%)
    stalled  — progress has not advanced for the warning threshold
    failed   — job failed / log error / critical stall

Routine status is generated hourly by the watchdog scheduler.

Standalone: reads kjob logs + kubectl only; never imports training code.
"""

from __future__ import annotations

import re
import json
import time
import pathlib
import datetime as dt
import subprocess

from tools.config import atomic_json, local_root, workspace_root

KJOB_LOG = workspace_root() / "Pi" / "kjob_logs"
OFFLINE_STATUS_SCHEMA = "pi.offline_eval_status.v1"
OFFLINE_STATUS_DIR = "offline_eval_status"
STATE_FILE = local_root() / "tools" / "watchdog-train" / "cursors.json"
TRAIN_PATTERNS = []
NATIVE_RUNS = []
KUBECTL_ARGS = []
ERROR_MARKERS = (
    "Traceback",
    "traceback",
    "Error",
    "ERROR",
    "RuntimeError",
    "OOM",
    "CUDA out of memory",
)
STALL_WARNING_MIN = 15
STALL_FAIL_MIN = 60


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (result.stdout or result.stderr).strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# kubectl job/pod facts
# ---------------------------------------------------------------------------


def _native_bindings() -> dict[str, dict]:
    """Bind native jobs to their own launch logs, never to a nearby log mtime."""
    bindings = {}
    for run in NATIVE_RUNS:
        root = pathlib.Path(run["artifact_dir"])
        for role in ("consumer", "producer"):
            try:
                job = (root / f"{role}-job-name.txt").read_text().strip()
                launch = (root / f"{role}-launch-id.txt").read_text().strip()
            except OSError:
                continue
            if not job or not launch:
                continue
            bindings[job] = {
                "exp_name": root.name,
                "run_id": str(root.resolve()),
                "role": role,
                "log_dir": root / "logs" / launch,
                "producers_per_node": run.get("producers_per_node", 1),
            }
    return bindings


def _job_phase(item: dict) -> str:
    status = item.get("status", {})
    conditions = {
        c["type"] for c in status.get("conditions", []) if c.get("status") == "True"
    }
    if conditions & {"Failed", "FailureTarget"}:
        return "failed"
    if conditions & {"Complete", "SuccessCriteriaMet"}:
        return "succeeded"
    # A failed attempt is not a terminal Job failure when Indexed jobs retry.
    # Likewise, one completed node is not a completed multi-node training run.
    expected = item.get("spec", {}).get("completions") or 1
    if status.get("succeeded", 0) >= expected:
        return "succeeded"
    if status.get("active", 0) > 0:
        return "active"
    return "pending"


def _kubectl_jobs() -> dict[str, dict]:
    """job_name -> {status, start_time, completion_time, exp_name}."""
    out = _run(["kubectl", *KUBECTL_ARGS, "get", "jobs", "-o", "json"])
    jobs: dict[str, dict] = {}
    bindings = _native_bindings()
    try:
        data = json.loads(out)
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            status = item.get("status", {})
            jobs[name] = {
                "status": _job_phase(item),
                "start_time": item["metadata"].get("creationTimestamp", ""),
                "ready": status.get("ready", 0),
                "expected": item.get("spec", {}).get("completions", 1),
                "failed_attempts": status.get("failed", 0),
                "reason": "; ".join(
                    c.get("message", c.get("reason", ""))
                    for c in status.get("conditions", [])
                    if c.get("status") == "True"
                    and c.get("type") in {"Failed", "FailureTarget"}
                ),
                "gpus_per_node": sum(
                    int(
                        c.get("resources", {})
                        .get("limits", {})
                        .get("nvidia.com/gpu", 0)
                    )
                    for c in item.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                ),
                # kjob records the launch script here; used for deterministic
                # exp-name resolution (see below).
                "script": item["metadata"]
                .get("annotations", {})
                .get("kjobctl.x-k8s.io/script", ""),
                "exp_name": "",  # resolved from the script first, then legacy mtime matching
                **bindings.get(name, {}),
            }
    except Exception:  # noqa: BLE001
        pass
    # Associate each job with a training/eval name by matching its submit
    # timestamp against kjob log files. The experiment identity lives inside
    # the container shell and is invisible to Kubernetes.
    import time as _time
    import calendar

    def _exp_by_timestamp(ts: str) -> tuple[str, pathlib.Path | None]:
        # creationTimestamp is UTC; timegm keeps UTC semantics so it is
        # comparable with log mtimes (absolute epochs).
        try:
            job_time = calendar.timegm(_time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return "", None
        best = ""
        best_delta = 3600  # 1h window
        best_log = None
        for name, pattern in TRAIN_PATTERNS:
            for log in KJOB_LOG.glob(pattern + ".out"):
                if log in taken:
                    continue
                delta = abs(log.stat().st_mtime - job_time)
                if delta < best_delta:
                    best_delta = delta
                    best = name
                    best_log = log
        for log in KJOB_LOG.glob("offline-eval-*.out"):
            if log in taken:
                continue
            delta = abs(log.stat().st_mtime - job_time)
            if delta < best_delta:
                best_delta = delta
                best = log.stem
                best_log = log
        return best, best_log

    # Preferred path: resolve the exp name from the launch script recorded in
    # the kjob annotation (deterministic pairing, no mtime guessing). Only
    # training exp scripts are eligible; everything else falls through to the
    # legacy mtime matching below.
    for job in jobs.values():
        if job.get("exp_name"):
            continue
        script_rel = job.get("script", "")
        if "/exp/" not in script_rel:
            continue
        try:
            text = (KJOB_LOG.parent / script_rel).read_text(errors="ignore")
        except OSError:
            continue
        match = re.search(r"export\s+EXP_NAME\s*=\s*[\"']?([^\"'\s]+)", text)
        if match:
            job["exp_name"] = match.group(1)
            continue
        # Serial launchers (e.g. launch_serial_abc.sh) set EXP_NAME only in the
        # sub-scripts they chain. Resolve the active phase: parse each
        # referenced sub-script's EXP_NAME and pick the one whose .out log has
        # the newest mtime (the 1h timestamp window below cannot pair a
        # multi-day serial job with its phase logs).
        sub_scripts = re.findall(r'zsh\s+"?\$SCRIPT_DIR/([^"\s]+\.sh)"?', text)
        best_name, best_mtime = "", 0.0
        for sub in sub_scripts:
            try:
                sub_text = (
                    (KJOB_LOG.parent / script_rel)
                    .parent.joinpath(sub)
                    .read_text(errors="ignore")
                )
            except OSError:
                continue
            sub_match = re.search(
                r"export\s+EXP_NAME\s*=\s*[\"']?([^\"'\s]+)", sub_text
            )
            if not sub_match:
                continue
            for log in KJOB_LOG.glob(f"jobs-*-{sub_match.group(1)}.out"):
                if log.stat().st_mtime > best_mtime:
                    best_mtime = log.stat().st_mtime
                    best_name = sub_match.group(1)
        if best_name:
            for display, pattern in TRAIN_PATTERNS:
                if pattern.strip("*") in best_name:
                    job["exp_name"] = display
                    break
            else:
                job["exp_name"] = best_name

    # Greedy assignment, oldest job first: each log file maps to one job only.
    # Two jobs submitted seconds apart (e.g. GOAI balanced + frame) otherwise
    # both resolve to the log with the closest mtime and collide.
    taken: set[pathlib.Path] = set()
    for job in sorted(jobs.values(), key=lambda j: j["start_time"]):
        if job.get("exp_name"):
            continue
        exp, log = _exp_by_timestamp(job["start_time"])
        job["exp_name"] = exp
        if log is not None:
            taken.add(log)
    return jobs


# ---------------------------------------------------------------------------
# log parsing
# ---------------------------------------------------------------------------


def _latest_err(pattern: str) -> pathlib.Path | None:
    errs = list(KJOB_LOG.glob(pattern + ".err"))
    if not errs:
        return None
    return max(errs, key=lambda p: p.stat().st_mtime)


def _latest_train_err(exp_name: str) -> pathlib.Path | None:
    """Return the rank-0 training log for a raw experiment name."""
    single_node_suffix = f"-{exp_name}.err"
    multi_node_prefix = f"jobs-{exp_name}-node0-"
    candidates = [
        path
        for path in KJOB_LOG.glob("jobs-*.err")
        if path.name.endswith(single_node_suffix)
        or path.name.startswith(multi_node_prefix)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _parse_elapsed_h(elapsed: str) -> float:
    parts = [int(p) for p in elapsed.split(":")]
    if len(parts) == 3:
        return parts[0] + parts[1] / 60 + parts[2] / 3600
    if len(parts) == 2:
        return parts[0] / 60 + parts[1] / 3600
    return parts[0] / 3600


def _tqdm_fields(err_path: pathlib.Path) -> dict | None:
    text = err_path.read_text(errors="ignore")
    matches = re.findall(
        r"FSDP-Train:\s+\S+\s+\|\s+(\d+)/(\d+)\s+\[(\d+(?::\d+){1,2})<(\d+(?::\d+){1,2}),\s+(\d+\.\d+)(s/it|it/s),\s*loss=([\d.e-]+),\s*lr=([\d.e-]+)",
        text,
    )
    if not matches:
        return None
    step, total, elapsed, _remaining, rate, unit, loss, lr = matches[-1]
    # tqdm prints "s/it" below 1 it/s and "it/s" above; normalize to it/s.
    its = float(rate) if unit == "it/s" else 1.0 / float(rate)
    spe = re.search(r"epochs=(\d+)\s+steps_per_epoch=(\d+)", text)
    return {
        "type": "train",
        "step": int(step),
        "total": int(total),
        "elapsed_h": _parse_elapsed_h(elapsed),
        "its": its,
        "loss": loss,
        "lr": lr,
        "epoch": (int(step) / int(spe.group(2)), int(spe.group(1)))
        if spe
        else (None, None),
    }


def _iso_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _offline_progress(exp_name: str, *, now: float | None = None) -> dict | None:
    status_path = KJOB_LOG / OFFLINE_STATUS_DIR / f"{exp_name}.json"
    try:
        payload = json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema") != OFFLINE_STATUS_SCHEMA:
        return None

    counts = payload.get("counts", {})
    try:
        total = int(counts["total"])
        pending = int(counts["pending"])
        running = int(counts["running"])
        complete = int(counts["complete"])
        skipped = int(counts["skipped"])
        failed = int(counts["failed"])
    except (KeyError, TypeError, ValueError):
        return None

    now = time.time() if now is None else now
    started_at = _iso_timestamp(payload.get("started_at"))
    finished_at = _iso_timestamp(payload.get("finished_at"))
    elapsed_seconds = max(0.0, (finished_at or now) - started_at) if started_at else 0.0
    jobs = payload.get("jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
    finished_durations = [
        float(job["elapsed_seconds"])
        for job in jobs.values()
        if isinstance(job, dict)
        and job.get("status") in {"complete", "failed"}
        and isinstance(job.get("elapsed_seconds"), (int, float))
    ]
    remaining = pending + running
    max_workers = max(1, int(payload.get("plan", {}).get("max_workers", 1)))
    if payload.get("status") in {"complete", "failed"}:
        remaining_hours = 0.0
    elif finished_durations:
        average_seconds = sum(finished_durations) / len(finished_durations)
        remaining_hours = average_seconds * remaining / max_workers / 3600
    else:
        remaining_hours = None

    running_jobs = [
        job_id
        for job_id, job in jobs.items()
        if isinstance(job, dict) and job.get("status") == "running"
    ]
    activity_times = [status_path.stat().st_mtime]
    for job_id in running_jobs:
        log_value = jobs[job_id].get("log")
        if not isinstance(log_value, str):
            continue
        try:
            activity_times.append(pathlib.Path(log_value).stat().st_mtime)
        except OSError:
            pass

    return {
        "type": "offline_eval",
        "request_id": str(payload.get("request_id", "")),
        "status": str(payload.get("status", "")),
        "step": complete + skipped + failed,
        "total": total,
        "pending": pending,
        "running": running,
        "complete": complete,
        "skipped": skipped,
        "failed": failed,
        "elapsed_h": elapsed_seconds / 3600,
        "remaining_h": remaining_hours,
        "running_jobs": running_jobs,
        "activity_age_min": max(0.0, (now - max(activity_times)) / 60),
    }


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save_state(state: dict) -> None:
    atomic_json(STATE_FILE, state)


# ---------------------------------------------------------------------------
# message building
# ---------------------------------------------------------------------------


def _event_body(
    job_id: str,
    exp_name: str,
    status: str,
    progress: dict | None,
    extra: list[tuple[str, str]] = (),
) -> str:
    # Same title + bullet layout as the heartbeat status(): name in the
    # title line, then kjob and progress details as bullets (no markdown
    # tables — cramped on phones).
    title = exp_name or job_id or "训练任务"
    kind = _job_kind(exp_name)
    lines = [f"**{title}** - {status}（{kind}）"]
    if job_id:
        lines.append(f"- kjob: {job_id}")
    if progress:
        _append_progress(lines, progress, precise=True)
    for key, value in extra:
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _job_kind(exp_name: str) -> str:
    if exp_name.startswith("offline-eval"):
        return "评测"
    if exp_name:
        return "训练"
    return "未知"


def _append_progress(lines: list[str], progress: dict, *, precise: bool) -> None:
    if progress.get("type") == "offline_eval":
        total = progress["total"]
        pct = progress["step"] / total * 100 if total else 0.0
        request_id = progress.get("request_id")
        if request_id:
            lines.append(f"- 请求: {request_id}")
        lines.append(f"- 进度 job: {progress['step']}/{total} ({pct:.1f}%)")
        lines.append(f"- 运行中: {progress['running']} / 失败: {progress['failed']}")
        remaining_h = progress.get("remaining_h")
        eta = f"{remaining_h:.1f}h" if remaining_h is not None else "计算中"
        lines.append(f"- ETA: 已跑 {progress['elapsed_h']:.1f}h / 剩 {eta}")
        running_jobs = progress.get("running_jobs", [])
        if running_jobs:
            visible = ", ".join(running_jobs[:3])
            if len(running_jobs) > 3:
                visible += f" 等 {len(running_jobs)} 个"
            lines.append(f"- 当前: {visible}")
        return

    if progress.get("type") == "susser_train":
        precise = True
        if not progress.get("step"):
            lines.append("- 训练: 尚未产出 step，正在启动或等待供数")
            return
        seconds = progress["step_seconds"]
        lines.append(f"- 步速: {seconds:.3f} s/step（最近日志窗口）")
        lines.append(f"- LR: {progress.get('lr', '?')}")
        age = max(0, time.time() - progress["last_step_time"])
        lines.append(f"- 最近训练进度: {age:.0f} 秒前")
        if age < STALL_WARNING_MIN * 60 and progress.get("total"):
            eta = (
                dt.datetime.fromtimestamp(
                    time.time() + max(0, progress["total"] - progress["step"]) * seconds
                )
                .astimezone()
                .strftime("%m-%d %H:%M %Z")
            )
            lines.append(f"- 预计完成: {eta}（按步速估计，另计保存和排队）")
        else:
            lines.append("- 预计完成: 等待进度恢复后重算")
        checkpoints = progress.get("checkpoints", {})
        if checkpoints:
            saved = " / ".join(
                f"{kind}: step {step}" for kind, step in checkpoints.items()
            )
            lines.append(f"- checkpoint 保存日志: {saved}（未自动核验 S3 完整性）")
        if not progress.get("total"):
            lines.append(f"- 进度 step: {progress['step']}（总步数尚未读到）")
            lines.append(f"- Loss: {progress['loss']}")
            return

    epoch_s = (
        f"{progress['epoch'][0]:.2f}/{progress['epoch'][1]}"
        if progress["epoch"][0] is not None and precise
        else f"{progress['epoch'][0]:.1f}/{progress['epoch'][1]}"
        if progress["epoch"][0] is not None
        else "?"
    )
    pct = progress["step"] / progress["total"] * 100
    remaining_h = max(0, progress["total"] - progress["step"]) / progress["its"] / 3600
    pct_text = f"{pct:.1f}" if precise else f"{pct:.0f}"
    lines.append(f"- 进度 epoch: {epoch_s}")
    lines.append(f"- 进度 step: {progress['step']}/{progress['total']} ({pct_text}%)")
    remaining = (
        "等待恢复"
        if progress.get("type") == "susser_train"
        and time.time() - progress["last_step_time"] >= STALL_WARNING_MIN * 60
        else f"{remaining_h:.1f}h"
    )
    lines.append(f"- ETA: 已跑 {progress['elapsed_h']:.1f}h / 剩 {remaining}")
    lines.append(f"- Loss: {progress['loss']}")


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


class TrainMonitor:
    name = "train"
    alert_interval = 120
    status_interval = 3600

    def __init__(self, sender=None) -> None:
        self.sender = sender or (lambda kind, body: False)
        self._err_state = _load_state()
        self._native_progress_state = self._err_state.setdefault("susser_progress", {})
        self._known_jobs: dict[str, str] = {}  # job_name -> last phase notified
        self._stalled_reported: dict[str, str] = {}
        self._failed_attempts: dict[str, int] = {}
        self._missing_jobs: set[str] = set()
        self._initialized = False  # first scan builds baseline, emits nothing

    # -- event scan ----------------------------------------------------------
    def check_alerts(self) -> list:
        events: list[
            tuple[str, str, str, dict | None, list]
        ] = []  # (kind, job_id, exp, progress, extra)

        jobs = _kubectl_jobs()
        # 1. start / finished / failed by job phase transitions
        for job_name, job in jobs.items():
            phase = job["status"]
            prev = self._known_jobs.get(job_name)
            exp = job.get("exp_name", "")
            progress_id = job.get("run_id") or exp
            extra = self._job_details(job_name, job, jobs) if job.get("role") else []
            if job.get("role"):
                self._failed_attempts.setdefault(job_name, job["failed_attempts"])
            if prev is None and phase in {"active", "pending"}:
                self._known_jobs[job_name] = "running"
                if not self._initialized:
                    continue  # baseline: existed before we started watching
                events.append(("start", job_name, exp, self._progress_of(progress_id), extra))
            elif prev in ("running", "active") and phase == "succeeded":
                self._known_jobs[job_name] = "finished"
                events.append(
                    ("finished", job_name, exp, self._progress_of(progress_id), extra)
                )
            elif prev in ("running", "active") and phase == "failed":
                self._known_jobs[job_name] = "failed"
                events.append(("failed", job_name, exp, self._progress_of(progress_id), extra))
            elif prev is None and phase == "failed":
                # appeared already failed (e.g. quick crash)
                self._known_jobs[job_name] = "failed"
                if not self._initialized:
                    continue
                events.append(("failed", job_name, exp, self._progress_of(progress_id), extra))
            if job.get("role"):
                failures = job["failed_attempts"]
                old_failures = self._failed_attempts.get(job_name, failures)
                self._failed_attempts[job_name] = failures
                if phase in {"active", "pending"} and failures > old_failures:
                    events.append(
                        (
                            "warning",
                            job_name,
                            exp,
                            self._progress_of(progress_id),
                            extra
                            + [
                                (
                                    "重试",
                                    f"失败尝试 {old_failures} → {failures}；Job 尚未终止",
                                )
                            ],
                        )
                    )
        for job_name, binding in _native_bindings().items():
            if job_name in jobs:
                self._missing_jobs.discard(job_name)
            elif (
                self._known_jobs.get(job_name) == "running"
                and job_name not in self._missing_jobs
                and any(
                    job.get("run_id") == binding["run_id"]
                    and job.get("status") in {"active", "pending"}
                    for job in jobs.values()
                )
            ):
                self._missing_jobs.add(job_name)
                events.append(
                    (
                        "failed",
                        job_name,
                        binding["exp_name"],
                        self._progress_of(binding["run_id"]),
                        [
                            (
                                "故障",
                                f"已监控的 {binding['role']} Job 消失，同一 run 的另一组仍在运行",
                            ),
                        ],
                    )
                )
        self._initialized = True
        # 2. log errors -> failed
        for kind, _, exp, progress, extra in self._scan_err_events(jobs):
            events.append((kind, "", exp, progress, extra))
        # 3. stall -> failed (after critical threshold)
        for kind, _, exp, progress, extra in self._check_stall_events(jobs):
            events.append((kind, "", exp, progress, extra))

        pending = self._err_state.setdefault("pending_events", [])
        pending.extend(events)
        # Log cursors and phase transitions can advance only if the generated
        # notifications are retained alongside them until delivery succeeds.
        _save_state(self._err_state)
        while pending:
            if not self._emit(*pending[0]):
                break
            pending.pop(0)
        _save_state(self._err_state)
        return []

    # -- log err scan --------------------------------------------------------
    def _scan_err_events(self, jobs=None):
        events = []
        # 训练日志 jobs-*.err + 离线评测日志 offline-eval-*.err（P5 接入）
        err_files = [
            (path, None)
            for path in sorted(
                list(KJOB_LOG.glob("jobs-*.err"))
                + list(KJOB_LOG.glob("offline-eval-*.err"))
            )
        ]
        for job_name, binding in _native_bindings().items():
            if jobs is not None and jobs.get(job_name, {}).get("status") not in {
                "active",
                "pending",
            }:
                continue
            err_files.extend(
                (path, binding)
                for path in sorted(
                    binding["log_dir"].glob(f"{binding['role']}-node_*.log")
                )
            )
        for err_path, binding in err_files:
            key = str(err_path)
            size = err_path.stat().st_size
            if key not in self._err_state:
                self._err_state[key] = size
                continue
            offset = self._err_state[key]
            if size <= offset:
                self._err_state[key] = size
                continue
            previous_line = ""
            with err_path.open("rb") as fh:
                if binding:
                    # Keep the preceding warning available across polls/restarts.
                    fh.seek(max(0, offset - 512))
                    context = fh.read(offset - fh.tell()).decode(errors="ignore")
                    context_lines = re.sub(r"\x1b\[[0-9;]*m", "", context).splitlines()
                    previous_line = context_lines[-1] if context_lines else ""
                fh.seek(offset)
                new_bytes = fh.read(size - offset)
            if binding:
                # Do not consume a warning/traceback header before its newline.
                complete = new_bytes.rfind(b"\n") + 1
                self._err_state[key] = offset + complete
                new_text = new_bytes[:complete].decode(errors="ignore")
            else:
                self._err_state[key] = size
                new_text = new_bytes.decode(errors="ignore")
            if binding:
                # These logs mix warnings, handled corrupt samples and real errors.
                # A retrying worker is an orange warning; Job conditions decide death.
                lines = re.sub(r"\x1b\[[0-9;]*m", "", new_text).splitlines()
                error_line = None
                for line in lines:
                    handled_tag_retry = (
                        binding["role"] == "producer"
                        and line == "Traceback (most recent call last):"
                        and previous_line
                        == "WARNING:lakestream.core:Transient failure while reading consumer tags; retrying"
                    )
                    previous_line = line
                    if (
                        not handled_tag_retry
                        and re.search(
                            r"Traceback \(most recent call last\)|CUDA out of memory|ChildFailedError|"
                            r" - ERROR - |^ERROR:|DATA_STALL",
                            line,
                        )
                        and not any(
                            marker in line
                            for marker in (
                                "CORRUPT_VIDEO_GOP",
                                "REPLACED_INVALID_SAMPLE",
                            )
                        )
                    ):
                        error_line = line
                        break
                if error_line:
                    exp = binding["exp_name"]
                    events.append(
                        (
                            "warning",
                            "",
                            exp,
                            self._progress_of(binding["run_id"]),
                            [
                                ("角色", binding["role"]),
                                ("日志", err_path.name),
                                ("异常日志", error_line.strip()[:200]),
                            ],
                        )
                    )
                continue
            for marker in ERROR_MARKERS:
                if marker in new_text:
                    line = next(
                        (ln for ln in new_text.splitlines() if marker in ln), marker
                    )
                    exp = self._exp_from_err_path(err_path)
                    events.append(
                        (
                            "failed",
                            "",
                            exp,
                            self._progress_of(exp),
                            [("错误", line.strip()[:200])],
                        )
                    )
                    break
        return events

    # -- stall ---------------------------------------------------------------
    def _check_stall_events(self, jobs: dict[str, dict]):
        events = []
        now = time.time()
        active_experiments = {
            str(job.get("run_id") or job["exp_name"]): str(job["exp_name"])
            for job in jobs.values()
            if job.get("status") == "active"
            and job.get("exp_name")
            and job.get("role") != "producer"
        }
        patterns = dict(TRAIN_PATTERNS)
        for name in list(self._stalled_reported):
            if name not in active_experiments:
                self._stalled_reported.pop(name, None)
        for identity, name in sorted(active_experiments.items()):
            if name.startswith("offline-eval-"):
                continue
            progress = self._progress_of(identity)
            if progress and progress.get("type") == "susser_train":
                if not progress.get("step"):
                    continue
                age_min = max(0, now - progress["last_step_time"]) / 60
            else:
                err_path = (
                    _latest_err(patterns[name])
                    if name in patterns
                    else _latest_train_err(name)
                )
                if err_path is None or not progress:
                    continue
                age_min = (now - err_path.stat().st_mtime) / 60
            previous = self._stalled_reported.get(identity)
            if age_min >= STALL_FAIL_MIN:
                if previous != "failed":
                    events.append(
                        (
                            "failed",
                            "",
                            name,
                            progress,
                            [("故障", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                self._stalled_reported[identity] = "failed"
            elif age_min >= STALL_WARNING_MIN:
                if previous not in {"warning", "failed"}:
                    events.append(
                        (
                            "stalled",
                            "",
                            name,
                            progress,
                            [("停滞", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                if previous != "failed":
                    self._stalled_reported[identity] = "warning"
            else:
                self._stalled_reported.pop(identity, None)
        active_offline = {
            exp for exp in active_experiments if exp.startswith("offline-eval-")
        }
        for exp in list(self._stalled_reported):
            if exp.startswith("offline-eval-") and exp not in active_offline:
                self._stalled_reported.pop(exp, None)
        for exp in sorted(active_offline):
            progress = self._progress_of(exp)
            if not progress:
                continue
            age_min = progress["activity_age_min"]
            previous = self._stalled_reported.get(exp)
            if age_min >= STALL_FAIL_MIN:
                if previous != "failed":
                    events.append(
                        (
                            "failed",
                            "",
                            exp,
                            progress,
                            [("故障", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                self._stalled_reported[exp] = "failed"
            elif age_min >= STALL_WARNING_MIN:
                if previous not in {"warning", "failed"}:
                    events.append(
                        (
                            "stalled",
                            "",
                            exp,
                            progress,
                            [("停滞", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                if previous != "failed":
                    self._stalled_reported[exp] = "warning"
            else:
                self._stalled_reported.pop(exp, None)
        return events

    # -- helpers -------------------------------------------------------------
    def _native_progress(self, path: pathlib.Path) -> dict | None:
        """Incrementally parse rank 0 metrics and synchronous checkpoint saves."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        key = str(path)
        state = self._native_progress_state.setdefault(key, {})
        if size < state.get("offset", 0):
            state.clear()
        with path.open("rb") as fh:
            fh.seek(state.get("offset", 0))
            text = state.pop("partial", "") + fh.read(
                size - state.get("offset", 0)
            ).decode(errors="replace")
        text, _, partial = text.rpartition("\n")
        state.update(offset=size, partial=partial)
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        for field in ("steps_per_epoch", "total_steps"):
            match = re.search(rf'"{field}"\s*:\s*(\d+)', text)
            if match:
                state[field] = int(match.group(1))
        wandb = re.search(r"https://wandb\.ai/[^\s]+/runs/[^\s]+", text)
        if wandb:
            state["wandb"] = wandb.group(0)
        for line in text.splitlines():
            if "[rank 0]" not in line:
                continue
            metric = re.search(
                r"step:\s*(\d+)\s+loss:\s*(\S+).*?step_time:\s*([\d.]+)s.*?lr:\s*(\S+)",
                line,
            )
            if metric and int(metric.group(1)) > state.get("step", 0):
                timestamp = (
                    dt.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                    .replace(tzinfo=dt.timezone.utc)
                    .timestamp()
                )
                seconds = float(metric.group(3))
                if seconds > 0:
                    speeds = state.setdefault("speeds", [])
                    speeds.append(seconds)
                    del speeds[:-50]
                    state.update(
                        step=int(metric.group(1)),
                        loss=metric.group(2),
                        lr=metric.group(4),
                        last_step_time=timestamp,
                    )
                    state.setdefault("first_step_time", timestamp)
            save = re.search(
                r"Saving the checkpoint (\d+) into .*?/(training|raw|ema)/step-\d+",
                line,
            )
            if save:
                state["saving"] = (save.group(2), int(save.group(1)))
            elif "Finished saving the checkpoint" in line and state.get("saving"):
                kind, step = state.pop("saving")
                state.setdefault("checkpoints", {})[kind] = step
        if not state.get("step"):
            return {"type": "susser_train", "step": 0}
        seconds = sum(state["speeds"]) / len(state["speeds"])
        per_epoch = state.get("steps_per_epoch", 0)
        total = state.get("total_steps", 0)
        return {
            **state,
            "type": "susser_train",
            "total": total,
            "step_seconds": seconds,
            "its": 1 / seconds,
            "elapsed_h": max(0, time.time() - state["first_step_time"]) / 3600,
            "epoch": (state["step"] / per_epoch, total / per_epoch)
            if per_epoch
            else (None, None),
        }

    @staticmethod
    def _job_details(job_name: str, job: dict, jobs: dict) -> list:
        extra = [("角色", job["role"])]
        for name, related in jobs.items():
            if (
                (related.get("run_id") or related.get("exp_name"))
                != (job.get("run_id") or job["exp_name"])
                or not related.get("role")
            ):
                continue
            role = related["role"]
            quantity = (
                related["gpus_per_node"]
                if role == "consumer"
                else related["producers_per_node"]
            )
            unit = "GPU" if role == "consumer" else "配置进程"
            extra.append(
                (
                    role,
                    f"{related['status']} · 就绪 {related['ready']}/{related['expected']} 节点"
                    f" × {quantity} {unit}/节点 · 失败尝试 {related['failed_attempts']} · {name}",
                )
            )
        for name, binding in _native_bindings().items():
            if binding["run_id"] == job.get("run_id") and name not in jobs:
                extra.append((binding["role"], f"当前查不到 Job · {name}"))
        if job.get("reason"):
            extra.append(("Kubernetes 原因", job["reason"][:400]))
        return extra

    def _exp_from_err_path(self, err_path: pathlib.Path) -> str:
        for name, pattern in TRAIN_PATTERNS:
            if pathlib.PurePath(err_path).match(pattern + ".err"):
                return name
        if err_path.name.startswith("offline-eval"):
            return err_path.stem  # offline-eval-<job-id>（离线评测任务）
        return err_path.name

    def _progress_of(self, exp_name: str) -> dict | None:
        if not exp_name:
            return None
        native = [
            binding for binding in _native_bindings().values()
            if binding["role"] == "consumer"
            and exp_name in {binding["run_id"], binding["exp_name"]}
        ]
        if len(native) == 1:
            return self._native_progress(native[0]["log_dir"] / "consumer-node_0.log")
        if native:
            return None  # a basename cannot identify multiple native runs
        if exp_name.startswith("offline-eval-"):
            return _offline_progress(exp_name)
        for name, pattern in TRAIN_PATTERNS:
            if name == exp_name or pattern.strip("*") in exp_name:
                err_path = _latest_err(pattern)
                return _tqdm_fields(err_path) if err_path else None
        # Jobs resolved via the kjob script annotation use raw experiment
        # names; resolve either the single-node log or the multi-node node0 log.
        err_path = _latest_train_err(exp_name)
        return _tqdm_fields(err_path) if err_path else None

    def _emit(
        self, kind: str, job_id: str, exp: str, progress: dict | None, extra: list
    ) -> bool:
        status = {
            "start": "开始",
            "finished": "结束",
            "failed": "故障",
            "stalled": "停滞",
            "warning": "警告",
        }.get(kind, kind)
        body = _event_body(job_id, exp, status, progress, extra)
        sent = self.sender(kind, body)
        outcome = "sent" if sent else "send failed"
        print(f"[watchdog] {kind} event {outcome}: {exp or job_id}", flush=True)
        return bool(sent)

    # -- routine heartbeat: running kjobs as title + bullet list (mobile-
    #    friendly; markdown tables are cramped on phones). Finished/failed
    #    jobs already got their dedicated start/finish/failure cards.
    def status(self) -> str:
        jobs = _kubectl_jobs()
        if not jobs:
            return ""
        active_jobs = sorted(
            (name, job)
            for name, job in jobs.items()
            if job["status"] in {"active", "pending"}
        )
        blocks: list[str] = []
        for name, job in active_jobs:
            exp = job.get("exp_name", "")
            if job.get("role") == "producer" and any(
                (related.get("run_id") or related.get("exp_name"))
                == (job.get("run_id") or exp)
                and related.get("role") == "consumer"
                and related.get("status") in {"active", "pending"}
                for related in jobs.values()
            ):
                continue
            kind = _job_kind(exp)
            progress = self._progress_of(job.get("run_id") or exp) if exp else None
            lines = [
                f"**job{len(blocks) + 1}: {exp or name}** - {'等待调度' if job['status'] == 'pending' else '运行中'}（{kind}）",
                f"- kjob: {name}",
            ]
            if progress:
                _append_progress(lines, progress, precise=False)
                if progress.get("wandb"):
                    lines.append(f"- W&B: {progress['wandb']}")
            if job.get("role"):
                lines.extend(
                    f"- {key}: {value}"
                    for key, value in self._job_details(name, job, jobs)
                )
            blocks.append("\n".join(lines))
        if not blocks:
            return ""
        return time.strftime("🕐 %Y-%m-%d %H:%M\n\n") + "\n\n".join(blocks)
