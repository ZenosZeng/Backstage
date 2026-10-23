"""Exercise the real service lifecycle with a local SSH executable shim."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from tools.config import ROOT
from tools.service import process_start, running_record


def test_cluster_daemon_start_reuse_read_restart_and_stop(tmp_path):
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    calls_path = tmp_path / "ssh-calls.jsonl"
    ssh = executable_dir / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "kind = 'resolve' if '-G' in sys.argv else 'stream'\n"
        "with open(os.environ['CLUSTER_TEST_SSH_CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps({'kind': kind, 'pid': os.getpid()}) + '\\n')\n"
        "if kind == 'resolve':\n"
        "    print('hostname 192.0.2.1')\n"
        "    sys.exit(0)\n"
        "while True:\n"
        "    sample = {'sampled_at': time.time(), 'cpu_percent': 12.5, 'cpu_count': 8,\n"
        "              'memory_total_mib': 16384, 'memory_used_mib': 4096,\n"
        "              'gpus': [{'index': 0, 'name': 'Synthetic GPU', 'utilization': 25,\n"
        "                        'utilization_peak': 30, 'memory_used_mib': 1024,\n"
        "                        'memory_total_mib': 8192, 'power_watts': 50}]}\n"
        "    print(json.dumps(sample), flush=True)\n"
        "    time.sleep(.1)\n",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    local = tmp_path / "local"
    config = tmp_path / "cluster.json"
    settings = {"hosts": [{"alias": "synthetic-node", "label": "Initial node"}],
                "interval_seconds": 2}
    config.write_text(json.dumps(settings), encoding="utf-8")
    directory = local / "tools/cluster-monitor"
    snapshot_path = directory / "snapshot.json"
    record_path = directory / "pid.json"
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(local),
           "PATH": str(executable_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
           "CLUSTER_TEST_SSH_CALLS": str(calls_path), "PYTHONDONTWRITEBYTECODE": "1"}

    def call(*args):
        result = subprocess.run(
            [sys.executable, "-m", "tools.cluster", "--config", str(config), *args],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def read_json(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def calls():
        if not calls_path.exists():
            return []
        return [json.loads(line) for line in calls_path.read_text().splitlines()]

    def wait_for(check, description):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(.05)
        log = directory / "service.log"
        details = log.read_text()[-5000:] if log.exists() else "no service log"
        raise AssertionError(f"Timed out waiting for {description}: {details}")

    def online_sample(label):
        snapshot = read_json(snapshot_path)
        if (snapshot and snapshot["summary"]["online"] == 1
                and snapshot["nodes"][0]["label"] == label):
            return snapshot
        return None

    stopped = False
    try:
        # Validation/inspection must not resolve SSH aliases or launch collectors.
        initial = json.loads(call("--once").stdout)
        assert initial["state"] == "no_data"
        assert calls() == []

        started = call()
        assert "started pid=" in started.stdout
        record = read_json(record_path)
        assert record and record["ready"]
        daemon_pid = record["pid"]
        generation = record["generation"]
        first = wait_for(lambda: online_sample("Initial node"), "an online sample")
        assert first["summary"]["gpus"] == 1
        assert first["summary"]["cpu_percent"] == 12.5
        initial_streams = [row["pid"] for row in calls() if row["kind"] == "stream"]
        assert len(initial_streams) == 1

        reused = call()
        assert "already running" in reused.stdout
        assert read_json(record_path)["pid"] == daemon_pid
        assert json.loads(call("--status", "--json").stdout)["running"] is True

        before_read = calls()
        inspected = json.loads(call("--once").stdout)
        assert inspected["summary"]["online"] == 1
        assert calls() == before_read

        # Re-exec must reload configuration while retaining the daemon's identity.
        settings["hosts"][0]["label"] = "Reloaded node"
        config.write_text(json.dumps(settings), encoding="utf-8")
        call("--restart", "--restart-timeout", "15")
        reloaded = read_json(record_path)
        assert reloaded["pid"] == daemon_pid
        assert reloaded["generation"] != generation
        wait_for(lambda: online_sample("Reloaded node"), "the reloaded sample")
        wait_for(lambda: all(process_start(pid) is None for pid in initial_streams),
                 "the previous SSH child to exit")
        streams = [row["pid"] for row in calls() if row["kind"] == "stream"]
        assert len(streams) == 2

        call("--stop")
        stopped = True
        wait_for(lambda: process_start(daemon_pid) is None, "the daemon to exit")
        wait_for(lambda: all(process_start(pid) is None for pid in streams),
                 "all SSH children to exit")
        assert json.loads(call("--status", "--json").stdout)["running"] is False
        assert read_json(snapshot_path)["state"] == "stopped"
        assert not record_path.exists()
    finally:
        if not stopped:
            try:
                call("--stop")
            except (AssertionError, subprocess.SubprocessError):
                pass
        current = running_record(directory, "cluster-monitor")
        if current:
            # Only target a service verified against this test's private PID record.
            try:
                os.kill(current["pid"], signal.SIGTERM)
                for _ in range(40):
                    if process_start(current["pid"]) != current["start_ticks"]:
                        break
                    time.sleep(.05)
                else:
                    if running_record(directory, "cluster-monitor") == current:
                        os.kill(current["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        # A failing lifecycle assertion must never leave the executable shim alive.
        for row in calls():
            if row["kind"] != "stream":
                continue
            pid = row["pid"]
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if str(ssh).encode() in command and process_start(pid) is not None:
                    os.kill(pid, signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                pass
