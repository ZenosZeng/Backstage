"""Read-only sampler streamed over SSH; no remote installation required."""

import argparse
import collections
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import threading
import time


def cpu_counters(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r"cpu\d*", fields[0]):
            continue
        values = [int(value) for value in fields[1:9]]
        result[fields[0]] = (sum(values), values[3] + values[4])
    return result


def cpu_usage(previous, current):
    def usage(key):
        if previous is None or key not in previous:
            return None
        total = current[key][0] - previous[key][0]
        idle = current[key][1] - previous[key][1]
        return round(max(0, min(100, 100 * (total - idle) / total)), 1) if total > 0 else None
    keys = sorted((key for key in current if key != "cpu"), key=lambda key: int(key[3:]))
    return usage("cpu"), [usage(key) for key in keys]


def memory_info(text):
    values = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
              for line in text.splitlines() if ":" in line and len(line.split()) >= 2}
    total, available = values["MemTotal"], values.get("MemAvailable", values["MemFree"])
    return {"total": total, "used": total - available, "percent": round(100 * (total - available) / total, 1)}


def number(value):
    try:
        parsed = float(value.strip())
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        return None


def gpu_info(text):
    rows = []
    for fields in csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(fields) != 10:
            continue
        index, uuid, name, utilization, used, total, temp, watts, limit, memory_utilization = [s.strip() for s in fields]
        rows.append({"index": int(index), "uuid": uuid, "name": name,
                     "utilization": number(utilization), "memory_used_mib": number(used),
                     "memory_total_mib": number(total), "temperature": number(temp),
                     "power_watts": number(watts), "power_limit_watts": number(limit),
                     "memory_utilization": number(memory_utilization)})
    return rows


GPU_FIELDS = ("index,uuid,name,utilization.gpu,memory.used,memory.total,"
              "temperature.gpu,power.draw,power.limit,utilization.memory")


class GpuSampler:
    """Keep nvidia-smi running so short kernels are not missed between ticks.

    A one-shot query reports the driver's own ~1 s window, so a GPU that only
    runs short kernels between ticks reads as 0 nearly every time. Sampling
    continuously and aggregating over a window gives a duty cycle and a peak.
    """

    def __init__(self, period=0.5, window=60.0):
        self.period = period
        self.window = window
        self.depth = max(2, int(round(window / period)))
        self.history = {}
        self.latest = {}
        self.lock = threading.Lock()
        self.process = None
        self.thread = None
        self.retry_at = 0.0
        self.received_at = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return bool(self.latest)
        with self.lock:
            self.latest.clear()
            self.history.clear()
            self.received_at = None
        try:
            self.process = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=" + GPU_FIELDS, "--format=csv,noheader,nounits",
                 "--loop-ms=%d" % max(100, int(self.period * 1000))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except (OSError, ValueError):
            self.process = None
            return False
        self.thread = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if self.latest:
                return True
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        self.stop()
        return False

    def ensure(self):
        """Return True when aggregated samples are available; retry spawns slowly."""
        if (self.thread is not None and self.thread.is_alive() and self.latest
                and self.received_at is not None and time.monotonic()-self.received_at <= 5):
            return True
        if self.thread is not None and self.thread.is_alive():
            self.stop()
        if time.monotonic() < self.retry_at:
            return False
        self.retry_at = time.monotonic() + 60
        return self.start()

    def _read(self, process):
        try:
            for line in process.stdout:
                rows = gpu_info(line)
                if not rows:
                    continue
                now = time.monotonic()
                with self.lock:
                    self.received_at = now
                    for row in rows:
                        self.latest[row["uuid"]] = row
                        series = self.history.get(row["uuid"])
                        if series is None:
                            series = self.history[row["uuid"]] = collections.deque(maxlen=self.depth)
                        series.append((now, row["utilization"]))
        except (OSError, ValueError):
            pass

    def stop(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                else:
                    process.wait()
            except OSError:
                pass
            finally:
                if process.stdout:
                    process.stdout.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.thread = None

    def snapshot(self):
        now, rows = time.monotonic(), []
        with self.lock:
            for row in self.latest.values():
                values = [value for when, value in self.history.get(row["uuid"], ())
                          if value is not None and now - when <= self.window]
                result = dict(row)
                result["utilization_instant"] = row["utilization"]
                result["utilization"] = round(statistics.mean(values), 1) if values else None
                result["utilization_peak"] = max(values) if values else None
                rows.append(result)
        return sorted(rows, key=lambda item: item["index"])


def endpoint(value):
    address, port = value.rsplit(":", 1)
    return address.strip("[]"), int(port)


def tcp_connections(text):
    """Parse ss -tinH without depending on process permissions or sudo."""
    rows, current = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if fields[0] == "ESTAB" or (len(fields) >= 4 and fields[0].isdigit() and fields[1].isdigit()):
            # With `state established`, ss omits the state column.
            if fields and fields[0] == "ESTAB":
                fields = fields[1:]
            if len(fields) < 4:
                current = None
                continue
            try:
                local_ip, port = endpoint(fields[2])
                peer_ip, peer_port = endpoint(fields[3])
            except (ValueError, IndexError):
                current = None
                continue
            current = {"port": port, "local_ip": local_ip, "peer_ip": peer_ip,
                       "peer_port": peer_port, "received": 0, "sent": 0}
            rows.append(current)
        if current is not None:
            for source, target in [("bytes_received", "received"), ("bytes_sent", "sent")]:
                match = re.search(r"\b" + source + r":(\d+)", line)
                if match:
                    current[target] = int(match.group(1))
    return rows


class BridgeLog:
    def __init__(self, source="auto", port_start=None):
        self.checked = 0
        self.source, self.port_start = source, port_start
        self.pid = self.path = self.identity = None
        self.position = 0

    def locate(self):
        if self.source != "auto":
            self.path = Path(self.source)
            return
        if time.monotonic() - self.checked < 20:
            return
        self.checked = time.monotonic()
        self.pid, self.path = None, None
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
                if ("public" in args and "--port-start" in args
                        and args[args.index("--port-start") + 1] == str(self.port_start) and "--log" in args):
                    self.pid, self.path = int(process.name), Path(args[args.index("--log") + 1])
                    break
            except (OSError, IndexError):
                continue

    def read(self):
        self.locate()
        if self.path is None:
            return [], {"pid": None, "log_available": False}
        try:
            stat = self.path.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}"
            if identity != self.identity or stat.st_size < self.position:
                self.identity = identity
                self.position = max(0, stat.st_size - 256 * 1024)
                with self.path.open("rb") as stream:
                    stream.seek(self.position)
                    if self.position:
                        stream.readline()
                    self.position = stream.tell()
            records = []
            with self.path.open("rb") as stream:
                stream.seek(self.position)
                for _ in range(12000):
                    offset = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    self.position = stream.tell()
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    event = item.get("event")
                    if event not in {"request", "prepared_request", "error", "prepared_error"}:
                        continue
                    records.append({"id": f"{identity}:{offset}", "time": item.get("time"),
                                    "event": event, "port": item.get("port"),
                                    "wire_bytes": item.get("wire_bytes", 0),
                                    "elapsed_ms": item.get("elapsed_ms", item.get("total_ms"))})
            return records, {"pid": self.pid, "log_available": True, "log_name": self.path.name}
        except OSError as error:
            return [], {"pid": self.pid, "log_available": False, "error": str(error)}


def network_counters():
    interfaces = []
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            interfaces.append(fields[0])
    counters = {}
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, raw = line.split(":", 1)
        if name.strip() in interfaces:
            fields = raw.split()
            counters[name.strip()] = {"rx": int(fields[0]), "tx": int(fields[8])}
    return counters


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--port-start", type=int)
    parser.add_argument("--port-end", type=int)
    parser.add_argument("--bridge-log")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval < 2:
        parser.error("interval must be finite and at least 2 seconds")
    if (args.port_start is None) != (args.port_end is None) or (args.port_start is not None
            and not 1 <= args.port_start <= args.port_end <= 65535):
        parser.error("port-start and port-end must define a valid port range")
    if args.bridge_log and args.port_start is None:
        parser.error("bridge-log requires a port range")
    previous_cpu, previous_net, previous_time = None, {}, None
    cpu_model = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                      if line.startswith("model name")), "CPU")
    bridge = BridgeLog(args.bridge_log, args.port_start) if args.bridge_log else None
    gpus = GpuSampler()
    def shutdown(*_):
        raise SystemExit(0)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, shutdown)
    try:
        gpus.start()
        while True:
            started = time.monotonic()
            sample = {"sampled_at": time.time(), "hostname": os.uname().nodename,
                      "cpu_model": cpu_model, "memory": memory_info(Path("/proc/meminfo").read_text()),
                      "load": list(os.getloadavg())}
            if gpus.ensure():
                sample.update(gpus=gpus.snapshot(), gpu_error=None, gpu_sampling="loop")
            else:
                sample.update(gpus=[], gpu_error=None, gpu_sampling="single")
                try:
                    query = subprocess.run(["nvidia-smi", "--query-gpu=" + GPU_FIELDS, "--format=csv,noheader,nounits"],
                                           capture_output=True, text=True, timeout=4)
                    sample["gpus"] = gpu_info(query.stdout) if query.returncode == 0 else []
                    sample["gpu_error"] = query.stderr.strip()[:300] if query.returncode else None
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    sample["gpu_error"] = str(error)[:300]
            current_cpu = cpu_counters(Path("/proc/stat").read_text())
            sample["cpu_percent"], sample["cpu_cores"] = cpu_usage(previous_cpu, current_cpu)
            sample["cpu_count"] = len(sample["cpu_cores"])
            current_net, now = network_counters(), time.monotonic()
            duration = now-previous_time if previous_time is not None else None
            sample["network"] = {"interfaces": list(current_net), "rx_bps": None, "tx_bps": None}
            if duration:
                for direction in ("rx", "tx"):
                    delta = sum(max(0, value[direction]-previous_net.get(name, value)[direction]) for name, value in current_net.items())
                    sample["network"][direction+"_bps"] = delta/duration
            previous_cpu, previous_net, previous_time = current_cpu, current_net, now
            if args.port_start is not None:
                events, info = bridge.read() if bridge else ([], {"pid": None, "log_available": False, "configured": False})
                sample["traffic"] = {"connections": [], "socket_error": None, "events": events, "bridge": info,
                                     "monitor_ip": os.environ.get("SSH_CONNECTION", "").split(" ")[0]}
                try:
                    sockets = subprocess.run(["ss", "-tinH", "state", "established", "(", "sport", ">=", f":{args.port_start}",
                                              "and", "sport", "<=", f":{args.port_end}", ")"],
                                             capture_output=True, text=True, timeout=3)
                    sample["traffic"]["connections"] = tcp_connections(sockets.stdout) if sockets.returncode == 0 else []
                    sample["traffic"]["socket_error"] = sockets.stderr.strip()[:200] if sockets.returncode else None
                except (OSError, subprocess.TimeoutExpired) as error:
                    sample["traffic"]["socket_error"] = str(error)[:200]
            try:
                print(json.dumps(sample, separators=(",", ":"), allow_nan=False), flush=True)
            except BrokenPipeError:
                return
            time.sleep(max(0.1, args.interval-(time.monotonic()-started)))
    finally:
        gpus.stop()


if __name__ == "__main__":
    main()
