import collections
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.cluster.monitor import Monitor, Traffic
from tools.cluster.collector import GpuSampler, cpu_counters, cpu_usage, gpu_info, memory_info, tcp_connections
from tools.cluster import collector
from tools.cluster.__main__ import factory
from tools.cluster.config import read_snapshot, settings_fingerprint, validate_config


class MetricsTests(unittest.TestCase):
    def test_cpu_delta_excludes_guest_double_count_and_counts_iowait_as_idle(self):
        before = cpu_counters("cpu 10 0 10 70 10 0 0 0 5 0\ncpu0 10 0 10 70 10 0 0 0 5 0")
        after = cpu_counters("cpu 30 0 20 120 30 0 0 0 8 0\ncpu0 30 0 20 120 30 0 0 0 8 0")
        total, cores = cpu_usage(before, after)
        self.assertEqual(total, 30)
        self.assertEqual(cores, [30])
        self.assertEqual(cpu_usage(None, after), (None, [None]))

    def test_memory_uses_available_not_merely_free(self):
        result = memory_info("MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 100 kB")
        self.assertEqual(result["percent"], 40)
        self.assertEqual(result["used"], 400 * 1024)

    def test_gpu_na_values_are_unknown_and_quoted_names_are_supported(self):
        result = gpu_info('0, GPU-1, "NVIDIA, test", 87, 12288, 24576, 64, 310.5, [N/A], NaN\n')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "NVIDIA, test")
        self.assertEqual(result[0]["utilization"], 87)
        self.assertIsNone(result[0]["power_limit_watts"])
        self.assertIsNone(result[0]["memory_utilization"])
        json.dumps(result, allow_nan=False)

    @staticmethod
    def gpu_row(uuid, utilization):
        return {"index": 0, "uuid": uuid, "name": "test", "utilization": utilization,
                "memory_used_mib": 1024.0, "memory_total_mib": 2048.0, "temperature": 40.0,
                "power_watts": 50.0, "power_limit_watts": 300.0, "memory_utilization": 0.0}

    def test_gpu_sampler_averages_short_bursts_a_single_query_would_miss(self):
        sampler = GpuSampler(period=0.5, window=60.0)
        now = time.monotonic()
        sampler.latest["GPU-1"] = self.gpu_row("GPU-1", 0.0)
        series = sampler.history.setdefault("GPU-1", collections.deque(maxlen=sampler.depth))
        for index in range(20):
            series.append((now - 2 + index * 0.1, 60.0 if index in (5, 12, 18) else 0.0))
        result = sampler.snapshot()[0]
        self.assertEqual(result["utilization_peak"], 60.0)
        self.assertEqual(result["utilization"], 9.0)
        self.assertEqual(result["utilization_instant"], 0.0)

    def test_gpu_sampler_ignores_samples_outside_the_window(self):
        sampler = GpuSampler(period=0.5, window=10.0)
        now = time.monotonic()
        sampler.latest["GPU-1"] = self.gpu_row("GPU-1", 0.0)
        series = sampler.history.setdefault("GPU-1", collections.deque(maxlen=sampler.depth))
        series.append((now - 100, 90.0))
        series.append((now - 1, 0.0))
        result = sampler.snapshot()[0]
        self.assertEqual(result["utilization_peak"], 0.0)
        self.assertEqual(result["utilization"], 0.0)

    def test_ss_ipv4_ipv6_and_optional_state_column(self):
        output = "  0  0  192.0.2.10:30001  203.0.113.8:50000\n\t cubic bytes_sent:900 bytes_received:1800\nESTAB 0 0 [::]:30002 [::ffff:203.0.113.9]:50001\n\t cubic bytes_sent:5 bytes_received:10\n"
        result = tcp_connections(output)
        self.assertEqual([item["port"] for item in result], [30001, 30002])
        self.assertEqual(result[0]["received"], 1800)
        self.assertEqual(result[1]["peer_ip"], "::ffff:203.0.113.9")

    @staticmethod
    def connection(received, sent=10, ip="203.0.113.8", port=30001):
        return {"local_ip": "192.0.2.10", "port": port, "peer_ip": ip,
                "peer_port": 50000, "received": received, "sent": sent}

    def test_traffic_baseline_deltas_and_known_source_labels(self):
        with patch("tools.cluster.monitor.time.time", return_value=100):
            traffic = Traffic({"203.0.113.8": "gpu-node-a"})
            traffic.update({"connections": [self.connection(1000)], "events": []}, 100)
            self.assertEqual(traffic.snapshot["received_bps"], 0)
        with patch("tools.cluster.monitor.time.time", return_value=105):
            traffic.update({"connections": [self.connection(2000, 110)], "events": []}, 105)
            source = traffic.snapshot["sources"][0]
            self.assertEqual(source["received_bps"], 200)
            self.assertEqual(source["sent_bps"], 20)
            self.assertEqual(source["received_bytes"], 1000)
            self.assertEqual(source["label"], "gpu-node-a")
            self.assertEqual(traffic.snapshot["by_port"], {30001: 1})

    def test_loopback_not_external_unknown_ip_not_attributed(self):
        with patch("tools.cluster.monitor.time.time", return_value=100):
            traffic = Traffic({})
            traffic.update({"connections": [self.connection(20, ip="127.0.0.1"),
                self.connection(30, ip="::ffff:203.0.113.9")], "events": []}, 100)
            self.assertEqual(traffic.snapshot["connections"], 1)
            self.assertEqual(traffic.snapshot["sources"][0]["ip"], "203.0.113.9")
            self.assertFalse(traffic.snapshot["sources"][0]["known"])

    def test_log_tail_reconnect_dedup_and_window_expiry(self):
        event = {"id": "inode:100", "time": 99, "event": "prepared_request", "elapsed_ms": 12}
        with patch("tools.cluster.monitor.time.time", return_value=100):
            traffic = Traffic({})
            traffic.update({"connections": [], "events": [event]}, 100)
            traffic.update({"connections": [], "events": [event]}, 105)
            self.assertEqual(traffic.snapshot["requests_60s"], 1)
            self.assertEqual(traffic.snapshot["request_p95_ms"], 12)
        with patch("tools.cluster.monitor.time.time", return_value=161):
            traffic.update({"connections": [], "events": []}, 161)
            self.assertEqual(traffic.snapshot["requests_60s"], 0)
            self.assertEqual(traffic.snapshot["last_request"], 99)

    def test_socket_failure_does_not_reset_byte_counter_baseline(self):
        with patch("tools.cluster.monitor.time.time", return_value=100):
            traffic = Traffic({})
            traffic.update({"connections": [self.connection(1000)], "events": []}, 100)
            traffic.update({"connections": [], "socket_error": "temporary", "events": []}, 105)
            traffic.update({"connections": [self.connection(2000)], "events": []}, 110)
            self.assertEqual(traffic.snapshot["received_bps"], 100)

    def test_stale_host_is_not_aggregated_as_current_idle_gpu(self):
        from subprocess import CompletedProcess
        with patch("tools.cluster.monitor.subprocess.run", return_value=CompletedProcess([],0,"hostname 203.0.113.8\n","")):
            monitor = Monitor({"hosts": [{"alias": "fixture-node"}]})
        with patch("tools.cluster.monitor.time.time", return_value=100):
            node = monitor.nodes["fixture-node"]
            node["received_at"] = 50
            node["sample"] = {"gpus": [{"utilization": 80, "memory_used_mib": 1000,
                "memory_total_mib": 24000, "power_watts": 100}], "cpu_percent": 50, "cpu_count": 64}
            result = monitor.snapshot()
            self.assertEqual(result["nodes"][0]["status"], "stale")
            self.assertEqual(result["summary"]["gpus"], 0)
            self.assertIsNone(result["summary"]["gpu_percent"])


if __name__ == "__main__":
    unittest.main()


SETTINGS = {"hosts": [{"alias": "fixture-node", "label": "测试节点", "group": "GPU"}]}


@pytest.mark.parametrize("change", [
    {}, {"hosts": []}, {"hosts": ["fixture-node"]}, {"hosts": [{"alias": "-oProxyCommand=bad"}]},
    {"hosts": [{"alias": "node"}, {"alias": "node"}]},
    SETTINGS | {"interval_seconds": float("nan")}, SETTINGS | {"interval_seconds": True},
    SETTINGS | {"health_interval_seconds": 1}, SETTINGS | {"interval_seconds": 10**500},
    SETTINGS | {"ports": {"host": "other", "start": 10, "end": 11}},
    SETTINGS | {"ports": {"host": [], "start": 10, "end": 11}},
    SETTINGS | {"ports": {"host": {}, "start": 10, "end": 11}},
    SETTINGS | {"ports": {"host": "fixture-node", "start": 1, "end": 300}},
    SETTINGS | {"ports": {"host": "fixture-node", "start": 10, "end": 11, "public_host": "http://example.test"}},
    SETTINGS | {"ports": {"host": "fixture-node", "start": 10, "end": 11, "bridge_log": "relative.log"}},
])
def test_cluster_settings_reject_invalid_values_without_side_effects(change):
    with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
        with pytest.raises(ValueError):
            validate_config(change)
        popen.assert_not_called()
        run.assert_not_called()


def test_factory_once_only_reads_existing_snapshot(tmp_path):
    directory = tmp_path / "not-created"
    with patch("subprocess.Popen") as popen, patch("subprocess.run") as run, patch("urllib.request.build_opener") as url:
        app = factory(SETTINGS, tmp_path / "config.json", directory)
        assert app.snapshot()["state"] == "no_data"
        assert not directory.exists()
        directory.mkdir()
        (directory / "snapshot.json").write_text("bad JSON")
        assert app.snapshot()["state"] == "unavailable"
        result = Monitor(SETTINGS).snapshot()
        (directory / "snapshot.json").write_text(json.dumps(result))
        assert app.snapshot() == result
        changed = factory(SETTINGS | {"interval_seconds": 10}, tmp_path / "config.json", directory)
        assert changed.snapshot()["state"] == "unavailable"
        popen.assert_not_called()
        run.assert_not_called()
        url.assert_not_called()


def test_snapshot_reader_distinguishes_missing_corrupt_and_valid(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_snapshot(tmp_path)
    for value in ([], {"schema_version": 1}, Monitor(SETTINGS).snapshot() | {"time": float("nan")},
                  Monitor(SETTINGS).snapshot() | {"nodes": ["bad"]},
                  Monitor(SETTINGS).snapshot() | {"state": []},
                  Monitor(SETTINGS).snapshot() | {"state": {}}):
        (tmp_path / "snapshot.json").write_text(json.dumps(value))
        with pytest.raises(ValueError):
            read_snapshot(tmp_path)
    result = Monitor(SETTINGS).snapshot()
    (tmp_path / "snapshot.json").write_text(json.dumps(result))
    assert read_snapshot(tmp_path) == result
    assert settings_fingerprint(SETTINGS) == settings_fingerprint(validate_config(SETTINGS))


def test_unconfigured_ports_do_not_collect_or_probe():
    monitor = Monitor(SETTINGS)
    command = shlex.split(monitor.command("fixture-node")[-1])
    assert "--port-start" not in command
    assert "--bridge-log" not in command
    assert monitor.snapshot()["ports"] is None
    monitor.stop.set()
    with patch("urllib.request.build_opener") as url:
        monitor.check_port(31000)
        url.assert_not_called()


def test_configured_ports_and_remote_log_path_use_configured_alias():
    monitor = Monitor(SETTINGS | {"ports": {"host": "fixture-node", "start": 31000, "end": 31001,
                                           "public_host": "203.0.113.8", "bridge_log": "/tmp/fixture bridge.jsonl"}})
    command = shlex.split(monitor.command("fixture-node")[-1])
    assert command[command.index("--port-start")+1] == "31000"
    assert command[command.index("--port-end")+1] == "31001"
    assert command[command.index("--bridge-log")+1] == "/tmp/fixture bridge.jsonl"
    assert [row["port"] for row in monitor.snapshot()["ports"]["checks"]] == [31000, 31001]


def test_live_ssh_without_samples_has_a_data_deadline():
    monitor = Monitor(SETTINGS)
    selector = MagicMock()
    selector.__enter__.return_value = selector
    selector.select.return_value = []
    with patch("tools.cluster.monitor.selectors.DefaultSelector", return_value=selector), patch(
        "tools.cluster.monitor.time.monotonic", side_effect=[0, 0, 30]
    ):
        with pytest.raises(ValueError, match="超时"):
            monitor._read_samples("fixture-node", SimpleNamespace(stdout=object(), stderr=object()))
    selector.select.assert_called_once()


def test_timeout_reaps_owned_process_and_reconnects():
    monitor = Monitor(SETTINGS)
    children = [object(), object()]
    def read(_host, process):
        if process is children[0]:
            raise ValueError("SSH 采集超时")
        monitor.stop.set()
    with patch.object(monitor, "_spawn", side_effect=children) as spawn, patch.object(
        monitor, "_read_samples", side_effect=read
    ), patch.object(monitor, "terminate") as terminate, patch.object(monitor.stop, "wait", return_value=False):
        monitor.collect("fixture-node")
    assert spawn.call_count == 2
    assert terminate.call_count == 2
    assert "超时" in monitor.nodes["fixture-node"]["error"]


def test_close_prevents_late_spawn_and_joins_owned_threads():
    monitor = Monitor(SETTINGS)
    thread = MagicMock()
    monitor.threads.append(thread)
    monitor.close()
    thread.join.assert_called_once()
    with patch("subprocess.Popen") as popen:
        assert monitor._spawn("fixture-node", ["unused"]) is None
        popen.assert_not_called()


def test_terminate_reaps_after_escalation_and_closes_pipes():
    process = MagicMock(pid=123)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 3), 0]
    with patch("os.killpg") as kill:
        Monitor.terminate(process)
    assert kill.call_count == 2
    assert process.wait.call_count == 2
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()


def test_run_stopped_before_start_does_not_contact_hosts(tmp_path):
    stop = threading.Event()
    stop.set()
    monitor = Monitor(SETTINGS)
    with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
        assert monitor.run(stop, tmp_path) == 0
        run.assert_not_called()
        popen.assert_not_called()
    assert read_snapshot(tmp_path)["state"] == "stopped"


def test_remote_gpu_sampler_reaps_and_joins():
    sampler = GpuSampler()
    sampler.process, sampler.thread = MagicMock(), MagicMock()
    process, thread = sampler.process, sampler.thread
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("nvidia-smi", 2), 0]
    sampler.stop()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    thread.join.assert_called_once()
    assert sampler.process is None and sampler.thread is None


def test_remote_collector_without_gpu_still_emits_cpu_and_cleans_up():
    sampler = MagicMock()
    sampler.ensure.return_value = False
    files = {"/proc/cpuinfo": "model name: fixture cpu", "/proc/meminfo": "MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 200 kB",
             "/proc/stat": "cpu 10 0 10 70 10 0 0 0\ncpu0 10 0 10 70 10 0 0 0"}
    emitted = []
    def output(text, **_):
        emitted.append(json.loads(text))
        raise BrokenPipeError
    with patch.object(collector, "GpuSampler", return_value=sampler), patch.object(
        Path, "read_text", lambda path, **_: files[str(path)]
    ), patch.object(collector, "network_counters", return_value={}), patch.object(
        collector.subprocess, "run", side_effect=FileNotFoundError("nvidia-smi missing")
    ), patch.object(collector.signal, "signal"), patch("builtins.print", side_effect=output):
        collector.main([])
    assert emitted[0]["gpus"] == []
    assert "missing" in emitted[0]["gpu_error"]
    assert emitted[0]["memory"]["percent"] == 40
    assert "traffic" not in emitted[0]
    sampler.stop.assert_called_once()


def test_bridge_log_emits_only_allowlisted_metadata(tmp_path):
    log = tmp_path / "bridge.jsonl"
    log.write_text(json.dumps({"event": "prepared_request", "time": 100, "port": 31000,
                               "wire_bytes": 200, "elapsed_ms": 2, "prompt": "private payload"})+"\n")
    events, info = collector.BridgeLog(str(log), 31000).read()
    assert info["log_available"]
    assert events[0]["port"] == 31000
    assert "prompt" not in events[0]
    assert "private payload" not in json.dumps(events)


def test_published_traffic_does_not_share_mutable_sources():
    monitor = Monitor(SETTINGS | {"ports": {"host": "fixture-node", "start": 31000, "end": 31000}})
    connection = {"local_ip": "192.0.2.1", "port": 31000, "peer_ip": "203.0.113.8",
                  "peer_port": 40000, "received": 100, "sent": 10}
    with patch("tools.cluster.monitor.time.time", return_value=100):
        monitor.traffic.update({"connections": [connection], "events": []}, 100)
        before = monitor.snapshot()
    with patch("tools.cluster.monitor.time.time", return_value=105):
        monitor.traffic.update({"connections": [connection | {"received": 300}], "events": []}, 105)
    assert before["ports"]["traffic"]["sources"][0]["received_bps"] == 0
    assert monitor.snapshot()["ports"]["traffic"]["sources"][0]["received_bps"] == 40


def test_active_gpus_only_counts_explicit_window_peaks_in_mixed_samples():
    monitor = Monitor({"hosts": [{"alias": name} for name in ("loop-node", "single-node", "unknown-node")]})
    samples = {
        "loop-node": {"gpu_sampling": "loop", "gpus": [
            MetricsTests.gpu_row("window-valid", 5) | {"utilization_peak": 15},
            MetricsTests.gpu_row("window-unknown-peak", 80) | {"utilization_peak": None},
        ]},
        "single-node": {"gpu_sampling": "single", "gpus": [
            MetricsTests.gpu_row("single", 90) | {"utilization_peak": 95},
        ]},
        "unknown-node": {"gpus": [MetricsTests.gpu_row("unknown", 95) | {"utilization_peak": 99}]},
    }
    for alias, sample in samples.items():
        monitor.nodes[alias].update(received_at=99, sample=sample)
    with patch("tools.cluster.monitor.time.time", return_value=100):
        summary = monitor.snapshot()["summary"]
    assert summary["gpus"] == 4
    assert summary["gpu_window_count"] == 2
    assert summary["gpu_single_count"] == 1
    assert summary["active_gpus"] == 1
    assert summary["gpu_percent"] == 67.5
