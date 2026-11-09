"""Low-frequency, independent disk reads for configured SSH hosts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
import os
import signal
import shlex
import subprocess
import threading
import time

INTERVAL = 300
REQUEST_FILE = 'disk-refresh.json'
# df runs in a bounded subprocess: a slow or stale mount cannot block GPU sampling.
REMOTE_SOURCE = '''import json, os, subprocess, sys, time
paths = json.loads(sys.argv[1])
result = {}
for key, path in paths.items():
    try:
        process = subprocess.run(['df', '-B1', '--output=size,used,avail,pcent,target', '--', path],
                                 capture_output=True, text=True, timeout=4, env={'LC_ALL':'C','PATH':'/usr/bin:/bin'})
        if process.returncode:
            raise ValueError(process.stderr.strip()[:200] or 'df failed')
        size, used, available, percent, mount = process.stdout.strip().splitlines()[-1].split(None, 4)
        if key == 'data' and os.path.normpath(mount) != os.path.normpath(path):
            raise ValueError('Configured data mount is not mounted at this path')
        result[key] = dict(path=path, state='available', total_bytes=int(size), used_bytes=int(used),
                           available_bytes=int(available), percent=float(percent.rstrip('%')),
                           mount=mount, sampled_at=time.time(), error=None)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result[key] = dict(path=path, state='error', error=str(error)[:200])
print(json.dumps(result, allow_nan=False))
'''


class DiskSampler:
    def __init__(self, hosts, config_hash):
        self.hosts = {h['alias']: h['disks'] for h in hosts if h.get('disks')}
        self.config_hash = config_hash
        self.lock = threading.Lock()
        self.processes = set()
        self.stop = threading.Event()
        self.values = {alias: {key: {'path': path, 'state': 'pending'} for key, path in paths.items()}
                       for alias, paths in self.hosts.items()}
        self.refreshing = False
        self.completed_request = None

    def snapshot(self):
        with self.lock:
            return {'interval': INTERVAL, 'refreshing': self.refreshing,
                    'completed_request': self.completed_request, 'nodes': copy.deepcopy(self.values)}

    def collect(self, alias):
        paths = self.hosts[alias]
        remote = shlex.join(['python3', '-c', REMOTE_SOURCE, json.dumps(paths)])
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                   '-o', 'LogLevel=ERROR', alias, remote]
        process = None
        try:
            with self.lock:
                if self.stop.is_set():
                    return
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                self.processes.add(process)
            stdout, stderr = process.communicate(timeout=15)
            if process.returncode:
                raise ValueError(stderr.decode(errors='replace').strip()[:200] or 'SSH disk read failed')
            result = json.loads(stdout)
            if not isinstance(result, dict) or set(result) != set(paths):
                raise ValueError('Invalid disk sample')
            for key, value in result.items():
                try:
                    if not isinstance(value, dict) or value.get('state') not in ('available', 'error'):
                        raise ValueError('Invalid disk sample')
                    if value['state'] == 'available':
                        for field in ('total_bytes', 'used_bytes', 'available_bytes', 'percent', 'sampled_at'):
                            number = value.get(field)
                            if (type(number) not in (int, float) or not math.isfinite(number)
                                    or field != 'available_bytes' and number < 0):
                                raise ValueError('Invalid disk numbers')
                except (ValueError, OverflowError) as error:
                    result[key] = {'state': 'error', 'error': str(error)}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result = {key: {'state': 'error', 'error': str(error)[:200]} for key in paths}
        finally:
            if process is not None:
                with self.lock:
                    self.kill_group(process)
                    self.processes.discard(process)
                # Never drain pipes here: descendants can retain them after SSH exits.
                try:
                    process.wait(timeout=3)
                finally:
                    for stream in (process.stdout, process.stderr):
                        if stream:
                            stream.close()
        with self.lock:
            for key, value in result.items():
                # Keep the original timestamp on failure; old values are explicitly stale.
                self.values[alias][key] = {**self.values[alias][key], **value, 'path': paths[key]}

    def request(self, directory):
        try:
            value = json.loads((directory / REQUEST_FILE).read_text())
            return value['requested_at'] if value.get('config_hash') == self.config_hash else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def run(self, directory):
        if not self.hosts:
            return
        deadline, seen = 0, None
        with ThreadPoolExecutor(max_workers=min(4, len(self.hosts))) as pool:
            while not self.stop.is_set():
                request = self.request(directory)
                if time.monotonic() >= deadline or request != seen:
                    deadline = time.monotonic() + INTERVAL
                    with self.lock:
                        self.refreshing = True
                    list(pool.map(self.collect, self.hosts))
                    with self.lock:
                        self.refreshing = False
                        self.completed_request = request
                    seen = request
                self.stop.wait(1)

    @staticmethod
    def kill_group(process):
        # Signal even when the SSH parent exited but its proxy still owns the pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        with self.lock:
            self.stop.set()
            for process in self.processes:
                self.kill_group(process)
