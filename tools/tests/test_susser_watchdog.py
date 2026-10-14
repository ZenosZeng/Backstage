"""Native Pi05 watchdog regressions, without training imports or notifications."""

import datetime as dt
import json
import os
import time

import pytest

from tools.watchdog import train
from tools.watchdog.__main__ import factory


@pytest.fixture
def native_run(tmp_path, monkeypatch):
    root = tmp_path / "native-run"
    root.mkdir()
    for role, job, launch in (
        ("consumer", "gpu-job", "gpu-launch"),
        ("producer", "cpu-job", "cpu-launch"),
    ):
        (root / f"{role}-job-name.txt").write_text(job)
        (root / f"{role}-launch-id.txt").write_text(launch)
        directory = root / "logs" / launch
        directory.mkdir(parents=True)
        (directory / f"{role}-node_0.log").write_text("")
    monkeypatch.setattr(
        train, "NATIVE_RUNS", [{"artifact_dir": root, "producers_per_node": 4}]
    )
    monkeypatch.setattr(train, "STATE_FILE", tmp_path / "cursors.json")
    monkeypatch.setattr(train, "KJOB_LOG", tmp_path / "old-logs")
    monkeypatch.setattr(train, "TRAIN_PATTERNS", [])
    return root


def _job(name, count, *, active=None, ready=None, failed=0, succeeded=0, conditions=()):
    return {
        "metadata": {"name": name, "creationTimestamp": "2026-10-10T15:00:00Z"},
        "spec": {
            "completions": count,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "resources": {
                                "limits": {
                                    "nvidia.com/gpu": "8" if name == "gpu-job" else "0",
                                }
                            }
                        }
                    ]
                }
            },
        },
        "status": {
            "active": count if active is None else active,
            "ready": count if ready is None else ready,
            "failed": failed,
            "succeeded": succeeded,
            "conditions": list(conditions),
        },
    }


def _metric(step, timestamp, *, rank=0, seconds=0.84):
    stamp = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S,%f"
    )[:23]
    return (
        f"{stamp} - [rank {rank}] susser_tod.infra.metrics - INFO - "
        f"\x1b[31mstep: {step:5}  \x1b[32mloss: 0.0456  "
        f"grad_norm: 0.1  step_time: {seconds:.4f}s  samples/s: 685  "
        "\x1b[36mlr: 1.800000e-05\x1b[39m\n"
    )


def _header(total=400, per_epoch=200):
    return (
        json.dumps({"steps_per_epoch": per_epoch, "total_steps": total}, indent=2)
        + "\n"
    )


def _save(step, kind, *, finished=True):
    prefix = "2026-10-10 15:46:56,396 - [rank 0] checkpoint.manager - INFO - "
    text = (
        prefix
        + f"Saving the checkpoint {step} into s3://bucket/run/ckpts/{kind}/step-{step}.\n"
    )
    if finished:
        text += prefix + "Finished saving the checkpoint in 10.64 seconds.\n"
    return text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"active": 17, "failed": 1}, "active"),
        ({"active": 2, "succeeded": 1}, "active"),
        ({"active": 0, "failed": 1}, "pending"),
        ({"active": 0, "succeeded": 3}, "succeeded"),
        (
            {"active": 2, "conditions": [{"type": "Failed", "status": "False"}]},
            "active",
        ),
        (
            {"active": 17, "conditions": [{"type": "FailureTarget", "status": "True"}]},
            "failed",
        ),
        ({"conditions": [{"type": "Failed", "status": "True"}]}, "failed"),
        ({"conditions": [{"type": "Complete", "status": "True"}]}, "succeeded"),
    ],
)
def test_indexed_job_phase_uses_terminal_conditions(status, expected):
    assert train._job_phase({"spec": {"completions": 3}, "status": status}) == expected


def test_native_job_binding_uses_exact_launch_files(native_run, monkeypatch):
    payload = {"items": [_job("gpu-job", 3), _job("cpu-job", 18, failed=1)]}
    monkeypatch.setattr(train, "_run", lambda _: json.dumps(payload))
    # A stale/newer neighboring launch must not replace the configured launch.
    wrong = native_run / "logs" / "other-launch"
    wrong.mkdir()
    (wrong / "consumer-node_0.log").write_text(_header() + _metric(999, time.time()))
    jobs = train._kubectl_jobs()
    assert jobs["gpu-job"]["exp_name"] == "native-run"
    assert jobs["gpu-job"]["log_dir"] == native_run / "logs" / "gpu-launch"
    assert jobs["gpu-job"]["gpus_per_node"] == 8
    assert jobs["cpu-job"]["role"] == "producer"
    assert jobs["cpu-job"]["status"] == "active"
    assert jobs["cpu-job"]["producers_per_node"] == 4


def test_native_metrics_rank_zero_budget_and_completed_saves(native_run):
    now = time.time()
    path = native_run / "logs/gpu-launch/consumer-node_0.log"
    path.write_text(
        _header()
        + _metric(10, now - 20, seconds=0.80)
        + _save(10, "training")
        + _save(10, "raw")
        + _save(10, "ema")
        + _metric(20, now - 10, seconds=0.88)
        + _metric(999, now, rank=7)
        + _save(20, "training", finished=False)
    )
    monitor = train.TrainMonitor()
    progress = monitor._progress_of("native-run")
    assert progress["step"] == 20
    assert progress["epoch"] == (0.1, 2)
    assert progress["total"] == 400
    assert progress["step_seconds"] == pytest.approx(0.84)
    assert progress["checkpoints"] == {"training": 10, "raw": 10, "ema": 10}
    assert progress["last_step_time"] == pytest.approx(now - 10, abs=0.002)
    lines = []
    train._append_progress(lines, progress, precise=True)
    assert any("0.840 s/step" in line for line in lines)
    assert any("20/400" in line for line in lines)
    assert any("未自动核验 S3" in line for line in lines)


def test_native_incremental_cursor_survives_restart_and_launch_change(
    native_run, monkeypatch
):
    path = native_run / "logs/gpu-launch/consumer-node_0.log"
    path.write_text(
        _header() + _metric(10, time.time()) + _save(10, "training", finished=False)
    )
    monkeypatch.setattr(
        train,
        "_run",
        lambda _: json.dumps({"items": [_job("gpu-job", 3), _job("cpu-job", 18)]}),
    )
    first = train.TrainMonitor()
    first.check_alerts()
    with path.open("a") as fh:
        fh.write(
            "2026-10-10 15:46:56,396 - [rank 0] manager - INFO - Finished saving the checkpoint in 1 seconds.\n"
        )
        fh.write(_metric(20, time.time()))
    resumed = train.TrainMonitor()
    progress = resumed._progress_of("native-run")
    assert progress["step"] == 20
    assert progress["total"] == 400  # header was before the persisted cursor
    assert progress["checkpoints"]["training"] == 10
    new = native_run / "logs/new-launch"
    new.mkdir()
    (new / "consumer-node_0.log").write_text(
        _header(1000, 500) + _metric(1, time.time())
    )
    (native_run / "consumer-launch-id.txt").write_text("new-launch")
    fresh = resumed._progress_of("native-run")
    assert fresh["step"] == 1
    assert fresh["total"] == 1000
    assert not fresh.get("checkpoints")


def test_new_log_output_cannot_hide_stalled_training(native_run, monkeypatch):
    now = time.time()
    monkeypatch.setattr(train.time, "time", lambda: now)
    path = native_run / "logs/gpu-launch/consumer-node_0.log"
    path.write_text(_header() + _metric(10, now - 1200) + "still printing heartbeat\n")
    os.utime(path, (now, now))
    monitor = train.TrainMonitor()
    jobs = {
        "gpu-job": {"exp_name": "native-run", "role": "consumer", "status": "active"}
    }
    assert [e[0] for e in monitor._check_stall_events(jobs)] == ["stalled"]
    assert monitor._check_stall_events(jobs) == []
    lines = []
    train._append_progress(lines, monitor._progress_of("native-run"), precise=True)
    assert any("等待恢复" in line for line in lines)
    assert not any("按步速估计" in line for line in lines)
    with path.open("a") as fh:
        fh.write(_metric(20, now))
    assert monitor._check_stall_events(jobs) == []
    assert "native-run" not in monitor._stalled_reported


def test_partial_metric_waits_for_the_complete_line(native_run):
    path = native_run / "logs/gpu-launch/consumer-node_0.log"
    path.write_text(_header() + _metric(10, time.time()))
    monitor = train.TrainMonitor()
    assert monitor._progress_of("native-run")["step"] == 10
    next_line = _metric(20, time.time())
    with path.open("a") as fh:
        fh.write(next_line[:80])
    assert monitor._progress_of("native-run")["step"] == 10
    with path.open("a") as fh:
        fh.write(next_line[80:])
    assert monitor._progress_of("native-run")["step"] == 20


def test_handled_decode_errors_and_deprecation_do_not_report_failure(native_run):
    path = native_run / "logs/cpu-launch/producer-node_0.log"
    monitor = train.TrainMonitor()
    assert monitor._scan_err_events() == []  # baseline
    with path.open("a") as fh:
        fh.write("CORRUPT_VIDEO_GOP error=RuntimeError decode failed\n")
        fh.write("REPLACED_INVALID_SAMPLE attempts=2\n")
        fh.write("FutureWarning: this will be an error in a future release\n")
    assert monitor._scan_err_events() == []
    with path.open("a") as fh:
        fh.write(
            "Traceback (most recent call last):\nRuntimeError: actual worker failure\n"
        )
    alerts = monitor._scan_err_events()
    assert [e[0] for e in alerts] == ["warning"]
    assert ("角色", "producer") in alerts[0][4]
    assert monitor._scan_err_events() == []


TAG_READ_RETRY = (
    "WARNING:lakestream.core:Transient failure while reading consumer tags; retrying\n"
)
TAG_READ_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "lakestream/core.py", line 184, in _consumer_watermarks\n'
    "    tags = self.ds.tags.list()\n"
    "RuntimeError: LanceError(Arrow): EOF while parsing an object at line 5 column 0\n"
)


@pytest.mark.parametrize("split", [0, 40, len(TAG_READ_RETRY), len(TAG_READ_RETRY) + 15])
def test_tag_read_retry_traceback_is_ignored_across_polls_and_restart(native_run, split):
    path = native_run / "logs/cpu-launch/producer-node_0.log"
    monitor = train.TrainMonitor()
    assert monitor._scan_err_events() == []
    text = TAG_READ_RETRY + TAG_READ_TRACE
    with path.open("a") as fh:
        fh.write(text[:split])
    assert monitor._scan_err_events() == []
    train._save_state(monitor._err_state)
    resumed = train.TrainMonitor()
    with path.open("a") as fh:
        fh.write(text[split:])
        fh.write("INFO:susser_tod.data.dataloader.lakestream:PRODUCER_BATCH_COMPLETE batch=17380\n")
    assert resumed._scan_err_events() == []
    assert resumed._err_state[str(path)] == path.stat().st_size


@pytest.mark.parametrize(
    "failure",
    [
        "Traceback (most recent call last):\nRuntimeError: tag read retries exhausted\n",
        "CUDA out of memory\n",
        "ChildFailedError: producer exited\n",
        "ERROR:lakestream.core:unexpected failure\n",
        "DATA_STALL waiting for producer\n",
    ],
)
def test_real_failure_after_handled_tag_retry_is_still_reported(native_run, failure):
    path = native_run / "logs/cpu-launch/producer-node_0.log"
    monitor = train.TrainMonitor()
    assert monitor._scan_err_events() == []
    with path.open("a") as fh:
        fh.write(TAG_READ_RETRY + TAG_READ_TRACE + failure)
    alerts = monitor._scan_err_events()
    assert [e[0] for e in alerts] == ["warning"]
    assert ("异常日志", failure.splitlines()[0]) in alerts[0][4]
    assert monitor._scan_err_events() == []


def test_retry_header_cannot_suppress_consumer_traceback(native_run):
    path = native_run / "logs/gpu-launch/consumer-node_0.log"
    monitor = train.TrainMonitor()
    assert monitor._scan_err_events() == []
    with path.open("a") as fh:
        fh.write(TAG_READ_RETRY + TAG_READ_TRACE)
    alerts = monitor._scan_err_events()
    assert [e[0] for e in alerts] == ["warning"]
    assert ("角色", "consumer") in alerts[0][4]


def test_producer_retry_then_terminal_failure_are_separate_events(
    native_run, monkeypatch
):
    items = [_job("gpu-job", 3), _job("cpu-job", 18)]
    monkeypatch.setattr(train, "_run", lambda _: json.dumps({"items": items}))
    sent = []
    monitor = train.TrainMonitor(
        sender=lambda kind, body: sent.append((kind, body)) or True
    )
    monitor.check_alerts()
    assert sent == []
    items[1] = _job("cpu-job", 18, active=17, ready=17, failed=1)
    monitor.check_alerts()
    assert [kind for kind, _ in sent] == ["warning"]
    assert "Job 尚未终止" in sent[-1][1]
    assert "就绪 17/18" in monitor.status()
    items[1] = _job(
        "cpu-job",
        18,
        failed=4,
        conditions=[
            {
                "type": "FailureTarget",
                "status": "True",
                "message": "Indexes failed: 7",
            }
        ],
    )
    monitor.check_alerts()
    assert [kind for kind, _ in sent] == ["warning", "failed"]
    assert "Indexes failed: 7" in sent[-1][1]


def test_removed_monitored_producer_is_reported_once(native_run, monkeypatch):
    items = [_job("gpu-job", 3), _job("cpu-job", 18)]
    monkeypatch.setattr(train, "_run", lambda _: json.dumps({"items": items}))
    sent = []
    monitor = train.TrainMonitor(
        sender=lambda kind, body: sent.append((kind, body)) or True
    )
    monitor.check_alerts()
    items.pop()
    monitor.check_alerts()
    monitor.check_alerts()
    assert [kind for kind, _ in sent] == ["failed"]
    assert "producer Job 消失" in sent[0][1]
    assert "当前查不到 Job" in monitor.status()


def test_status_groups_native_jobs_and_exposes_waiting_nodes(native_run, monkeypatch):
    monkeypatch.setattr(
        train,
        "_run",
        lambda _: json.dumps(
            {
                "items": [
                    _job("gpu-job", 3, ready=2),
                    _job("cpu-job", 18),
                ]
            }
        ),
    )
    status = train.TrainMonitor().status()
    assert status.count("**job1:") == 1
    assert "job2" not in status
    assert "尚未产出 step" in status
    assert "就绪 2/3 节点 × 8 GPU/节点" in status
    assert "就绪 18/18 节点 × 4 配置进程/节点" in status


@pytest.mark.parametrize(
    "runs",
    [
        "path",
        [{}],
        [{"artifact_dir": ""}],
        [{"artifact_dir": "run", "producers_per_node": 0}],
    ],
)
def test_invalid_native_config_is_rejected(tmp_path, runs):
    with pytest.raises(ValueError, match="native_runs"):
        factory("train")(
            {"notify": False, "native_runs": runs}, tmp_path / "config.json", tmp_path
        )
