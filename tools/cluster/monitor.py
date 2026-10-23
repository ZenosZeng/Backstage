"""Own SSH collection streams and publish read-only, machine-local snapshots."""

from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import copy
from http.client import HTTPException
import ipaddress
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import signal
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request

from tools.cluster.config import settings_fingerprint, validate_config
from tools.config import atomic_json


def normalized_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return str(address.ipv4_mapped) if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else str(address)
    except ValueError:
        return value


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)] if values else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A health check must report the configured endpoint's own response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Traffic:
    def __init__(self, known_ips):
        self.known_ips = known_ips
        self.previous, self.previous_time = {}, None
        self.sources = {}
        self.events, self.event_ids = deque(), set()
        self.last_request = None
        self.snapshot = None
        self.observed_since = time.time()

    def update(self, traffic, sampled_at):
        now = time.time()
        duration = sampled_at - self.previous_time if self.previous_time is not None else None
        if duration is not None and duration <= 0:
            return
        current, by_port = {}, Counter()
        for item in self.sources.values():
            item.update(connections=0, ports=[], received_bps=0, sent_bps=0)
        for connection in traffic.get("connections", []):
            ip = normalized_ip(connection["peer_ip"])
            try:
                if ipaddress.ip_address(ip).is_loopback:
                    continue
            except ValueError:
                pass
            key = (connection["local_ip"], connection["port"], ip, connection["peer_port"])
            current[key] = connection
            if ip not in self.sources:
                label = self.known_ips.get(ip)
                if not label and ip == normalized_ip(traffic.get("monitor_ip", "")):
                    label = "本机监控出口"
                self.sources[ip] = {"ip": ip, "label": label or "未识别来源", "known": bool(label),
                                    "connections": 0, "ports": [], "first_seen": now,
                                    "last_seen": now, "received_bps": 0, "sent_bps": 0,
                                    "received_bytes": 0, "sent_bytes": 0}
            source = self.sources[ip]
            source["connections"] += 1
            source["last_seen"] = now
            source["ports"].append(connection["port"])
            by_port[connection["port"]] += 1
            if duration is not None:
                old = self.previous.get(key)
                for field in ("received", "sent"):
                    amount = connection[field] - old[field] if old else connection[field]
                    amount = connection[field] if amount < 0 else amount
                    source[field + "_bytes"] += amount
                    source[field + "_bps"] += amount / duration
        if not traffic.get("socket_error"):
            self.previous, self.previous_time = current, sampled_at
        for identifier, source in list(self.sources.items()):
            source["ports"] = sorted(set(source["ports"]))
            if source["last_seen"] < now - 3600:
                del self.sources[identifier]
        for event in traffic.get("events", []):
            when = event.get("time")
            if not isinstance(when, (float, int)):
                continue
            if event["event"] in {"request", "prepared_request"}:
                self.last_request = max(self.last_request or 0, when)
            if when >= now - 60 and event["id"] not in self.event_ids:
                self.events.append(event)
                self.event_ids.add(event["id"])
        # Keep a genuine 60-second window even if reconnect tail reads arrive out of order.
        self.events = deque(event for event in self.events if event["time"] >= now - 60)
        self.event_ids = {event["id"] for event in self.events}
        requests = [event for event in self.events if event["event"] in {"request", "prepared_request"}]
        latencies = [event["elapsed_ms"] for event in requests if isinstance(event.get("elapsed_ms"), (int, float))]
        sources = sorted(self.sources.values(), key=lambda source: (-source["connections"], -source["last_seen"]))
        self.snapshot = {"sampled_at": sampled_at, "observed_since": self.observed_since,
                         "connections": sum(by_port.values()), "by_port": dict(by_port), "sources": sources,
                         "received_bps": sum(source["received_bps"] for source in sources),
                         "sent_bps": sum(source["sent_bps"] for source in sources),
                         "requests_60s": len(requests), "request_rps": len(requests) / 60,
                         "errors_60s": len(self.events) - len(requests),
                         "request_p50_ms": percentile(latencies, .5), "request_p95_ms": percentile(latencies, .95),
                         "last_request": self.last_request,
                         "bridge": traffic.get("bridge"), "socket_error": traffic.get("socket_error"),
                         "window_seconds": 60}


class Monitor:
    """Construct without I/O; only start/run contacts configured hosts."""

    def __init__(self, config):
        self.config = validate_config(config)
        self.config_hash = settings_fingerprint(self.config)
        self.interval = self.config["interval_seconds"]
        self.health_interval = self.config["health_interval_seconds"]
        self.hosts = [item["alias"] for item in self.config["hosts"]]
        self.nodes = {item["alias"]: {**item, "address": None, "received_at": None,
                                     "sample": None, "error": None, "history": deque(maxlen=120)}
                      for item in self.config["hosts"]}
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.processes, self.threads = {}, []
        self.known_ips = {}
        self.traffic = Traffic(self.known_ips)
        self.traffic_received_at = None
        self.ports = self.config["ports"]
        self.port_health = ({port: {"port": port, "status": "pending", "checked_at": None}
                             for port in range(self.ports["start"], self.ports["end"]+1)} if self.ports else {})
        self.public_host = self.ports["public_host"] if self.ports else None
        self.started = False

    def start(self):
        with self.lock:
            if self.started or self.stop.is_set():
                return
            self.started = True
        # SSH config resolution can execute Match rules; it belongs to explicit start.
        for host in self.hosts:
            if self.stop.is_set():
                return
            try:
                result = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    raise ValueError("无法读取 SSH 别名配置")
                settings = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
                address = settings.get("hostname", host)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                address = host
                with self.lock:
                    self.nodes[host]["error"] = str(error)[:300]
            with self.lock:
                self.nodes[host]["address"] = address
                self.known_ips[normalized_ip(address)] = self.nodes[host]["label"]
        if self.ports and not self.public_host:
            self.public_host = self.nodes[self.ports["host"]]["address"]
        for index, host in enumerate(self.hosts):
            with self.lock:
                if self.stop.is_set():
                    return
                thread = threading.Thread(target=self.collect, args=(host, index*.5), daemon=True)
                self.threads.append(thread)
                thread.start()
        if self.ports:
            with self.lock:
                if not self.stop.is_set():
                    thread = threading.Thread(target=self.health_loop, daemon=True)
                    self.threads.append(thread)
                    thread.start()

    def command(self, host):
        source = Path(__file__).with_name("collector.py").read_text(encoding="utf-8")
        remote = ["python3", "-u", "-c", source, "--interval", str(self.interval)]
        if self.ports and host == self.ports["host"]:
            remote.extend(["--port-start", str(self.ports["start"]), "--port-end", str(self.ports["end"])])
            if self.ports["bridge_log"]:
                remote.extend(["--bridge-log", self.ports["bridge_log"]])
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=8", "-o", "ServerAliveCountMax=2",
                "-o", "LogLevel=ERROR", host, shlex.join(remote)]

    def _spawn(self, host, command):
        # close sets stop under this same lock, so no process can miss its snapshot.
        with self.lock:
            if self.stop.is_set():
                return None
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       bufsize=0, start_new_session=True)
            self.processes[host] = process
            return process

    def _accept(self, host, sample):
        if (not isinstance(sample, dict) or type(sample.get("sampled_at")) not in (int, float)
                or not math.isfinite(sample["sampled_at"]) or not isinstance(sample.get("gpus"), list)):
            raise ValueError("SSH 采集返回无效样本")
        json.dumps(sample, allow_nan=False)
        if any(not isinstance(gpu, dict) or "utilization" not in gpu for gpu in sample["gpus"]):
            raise ValueError("SSH 采集返回无效 GPU 样本")
        with self.lock:
            node = self.nodes[host]
            node.update(sample=sample, received_at=time.time(), error=None)
            utils = [gpu["utilization"] for gpu in sample["gpus"] if gpu.get("utilization") is not None]
            node["history"].append({"time": sample["sampled_at"], "cpu": sample.get("cpu_percent"),
                                    "gpu": statistics.mean(utils) if utils else None})
            if self.ports and host == self.ports["host"] and "traffic" in sample:
                self.traffic.update(sample["traffic"], sample["sampled_at"])
                self.traffic_received_at = time.time()

    def _read_samples(self, host, process):
        # ServerAlive detects dead SSH transport, not a live but hung collector.
        timeout = max(20, self.interval*3+8)
        deadline, pending, error_tail = time.monotonic()+timeout, b"", b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while not self.stop.is_set():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise ValueError("SSH 采集超时：持续未收到有效样本，正在重连")
                for key, _ in selector.select(min(.2, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if key.data == "stderr":
                        error_tail = (error_tail+chunk)[-500:]
                        if not chunk:
                            selector.unregister(key.fileobj)
                        continue
                    if not chunk:
                        raise ValueError(error_tail.decode(errors="replace").strip() or "SSH 采集连接已关闭")
                    pending += chunk
                    if len(pending) > 4*1024*1024:
                        raise ValueError("SSH 采集样本过大")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        sample = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限采集值")))
                        self._accept(host, sample)
                        deadline = time.monotonic()+timeout

    def collect(self, host, delay=0):
        if self.stop.wait(delay):
            return
        command = self.command(host)
        while not self.stop.is_set():
            process = None
            try:
                process = self._spawn(host, command)
                if process is None:
                    return
                self._read_samples(host, process)
            except (OSError, ValueError, TypeError, KeyError) as error:
                with self.lock:
                    self.nodes[host]["error"] = str(error)[:300]
            finally:
                if process is not None:
                    self.terminate(process)
                with self.lock:
                    if self.processes.get(host) is process:
                        self.processes.pop(host, None)
            self.stop.wait(self.interval)

    @staticmethod
    def terminate(process):
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            else:
                process.wait()
        except ProcessLookupError:
            process.wait(timeout=3)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

    def check_port(self, port):
        if self.stop.is_set():
            return
        started = time.monotonic()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            host = f"[{self.public_host}]" if ":" in self.public_host else self.public_host
            request = urllib.request.Request(f"http://{host}:{port}{self.ports['health_path']}",
                                             headers={"User-Agent": "BackstageCluster/1"})
            with opener.open(request, timeout=3) as response:
                body = response.read(1024).strip()
                ok = response.status == 200 and body == b"OK"
                result = {"port": port, "status": "up" if ok else "down", "http_status": response.status,
                          "latency_ms": round((time.monotonic()-started)*1000, 1),
                          "checked_at": time.time(), "error": None if ok else "健康响应不符合预期"}
        except (OSError, ValueError, HTTPException) as error:
            result = {"port": port, "status": "down", "checked_at": time.time(), "error": str(error)[:200]}
            if isinstance(error, urllib.error.HTTPError):
                result["http_status"] = error.code
                error.close()
        with self.lock:
            self.port_health[port] = result

    def health_loop(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            while not self.stop.is_set():
                started = time.monotonic()
                list(pool.map(self.check_port, self.port_health))
                self.stop.wait(max(1, self.health_interval-(time.monotonic()-started)))

    def snapshot(self):
        now = time.time()
        with self.lock:
            nodes, gpus, window_gpus = [], [], []
            single_count = 0
            cpu_total, cpu_count = 0, 0
            for host in self.hosts:
                node = self.nodes[host]
                fresh = node["received_at"] is not None and now-node["received_at"] < self.interval*3+5
                status = "online" if fresh and not node["error"] else "stale" if node["sample"] else "connecting" if not node["error"] else "offline"
                sample = node["sample"]
                nodes.append({**{key: value for key, value in node.items() if key not in {"history", "sample"}},
                              "sample": {key: value for key, value in sample.items() if key != "traffic"} if sample else None,
                              "history": list(node["history"]), "status": status})
                if status == "online" and sample:
                    node_gpus = sample.get("gpus", [])
                    gpus.extend(node_gpus)
                    if sample.get("gpu_sampling") == "loop":
                        window_gpus.extend(node_gpus)
                    elif sample.get("gpu_sampling") == "single":
                        single_count += len(node_gpus)
                    if sample.get("cpu_percent") is not None:
                        cpu_total += sample["cpu_percent"]*sample["cpu_count"]
                        cpu_count += sample["cpu_count"]
            gpu_utils = [gpu["utilization"] for gpu in gpus if gpu["utilization"] is not None]
            gpu_peaks = [gpu["utilization_peak"] for gpu in window_gpus
                         if type(gpu.get("utilization_peak")) in (int, float)
                         and 0 <= gpu["utilization_peak"] <= 100]
            ports = None
            if self.ports:
                ports = {"host": self.public_host, "checks": list(self.port_health.values()),
                         "traffic": copy.deepcopy(self.traffic.snapshot),
                         "traffic_stale": self.traffic_received_at is None or now-self.traffic_received_at > self.interval*3+5}
            return {"schema_version": 1, "config_hash": self.config_hash,
                    "state": "stopped" if self.stop.is_set() else "collecting", "time": now,
                    "interval": self.interval, "health_interval": self.health_interval,
                    "nodes": nodes, "summary": {"online": sum(node["status"] == "online" for node in nodes),
                    "nodes": len(nodes), "gpus": len(gpus), "cpu_percent": cpu_total/cpu_count if cpu_count else None,
                    "gpu_percent": statistics.mean(gpu_utils) if gpu_utils else None,
                    "active_gpus": sum(value >= 10 for value in gpu_peaks),
                    "gpu_window_count": len(window_gpus), "gpu_single_count": single_count,
                    "gpu_memory_used_mib": sum(gpu["memory_used_mib"] or 0 for gpu in gpus),
                    "gpu_memory_total_mib": sum(gpu["memory_total_mib"] or 0 for gpu in gpus),
                    "power_watts": sum(gpu["power_watts"] or 0 for gpu in gpus)}, "ports": ports}

    def close(self):
        with self.lock:
            self.stop.set()
            processes, threads = list(self.processes.values()), list(self.threads)
        # Signal every owned session first, then let collectors reap their children.
        for process in processes:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic()+8
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline-time.monotonic()))
        for process in processes:
            if process.poll() is None:
                self.terminate(process)

    def run(self, stop, directory):
        try:
            if stop.is_set():
                self.stop.set()
            self.start()
            while not stop.is_set() and not self.stop.is_set():
                atomic_json(directory / "snapshot.json", self.snapshot())
                stop.wait(self.interval)
            return 0
        finally:
            self.close()
            atomic_json(directory / "snapshot.json", self.snapshot())
