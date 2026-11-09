"""Disk sampling is bounded, separate from GPU reads, and configuration-only."""
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from tools.cluster.config import validate_config, settings_fingerprint
from tools.cluster.disks import DiskSampler, REMOTE_SOURCE, REQUEST_FILE
from tools.cluster.monitor import Monitor
from tools.config import atomic_json


def settings():
    return validate_config({'hosts': [{'alias': 'demo', 'disks': {'home': '/home/demo', 'data': '/data'}}]})


def test_disk_paths_are_explicit_and_part_of_fingerprint():
    config = settings()
    assert config['hosts'][0]['disks']['home'] == '/home/demo'
    before = settings_fingerprint(config)
    config['hosts'][0]['disks']['data'] = '/mnt/disk'
    assert settings_fingerprint(config) != before
    for disks in ({'data': '-bad'}, {'home': '~/home'}, {'home': '/bad\npath'}, {'command': '/data'}, []):
        with pytest.raises(ValueError):
            validate_config({'hosts': [{'alias': 'demo', 'disks': disks}]})
    assert 'disks' not in validate_config({'hosts': [{'alias': 'demo'}]})['hosts'][0]


def remote_sample(monkeypatch, capsys, run):
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['probe', json.dumps({'home': '/home/demo', 'data': '/data'})])
    exec(REMOTE_SOURCE, {})
    return json.loads(capsys.readouterr().out)


def test_remote_df_reads_capacity_and_rejects_unmounted_data(monkeypatch, capsys):
    def run(args, **kwargs):
        assert kwargs['timeout'] == 4
        assert args[-2:] in (['--', '/home/demo'], ['--', '/data'])
        return SimpleNamespace(returncode=0, stdout='Size Used Avail Use% Mounted\n1000 800 150 85% /\n')
    data = remote_sample(monkeypatch, capsys, run)
    assert data['home']['used_bytes'] == 800
    assert data['home']['available_bytes'] == 150
    assert data['home']['percent'] == 85  # df includes reserved blocks in its usage denominator.
    assert data['data']['state'] == 'error'
    assert 'not mounted' in data['data']['error']


def test_remote_df_timeout_does_not_prevent_other_path(monkeypatch, capsys):
    def run(args, **kwargs):
        if args[-1] == '/home/demo':
            raise subprocess.TimeoutExpired(args, 4)
        return SimpleNamespace(returncode=0, stdout='Size Used Avail Use% Mounted\n1000 500 500 50% /data\n')
    data = remote_sample(monkeypatch, capsys, run)
    assert data['home']['state'] == 'error'
    assert data['data']['percent'] == 50


def test_failure_retains_original_disk_timestamp(monkeypatch):
    sampler = DiskSampler(settings()['hosts'], 'a' * 64)
    sampler.values['demo']['home'].update(percent=42, sampled_at=123, state='available')
    def fail(*args, **kwargs):
        raise OSError('unreachable')
    monkeypatch.setattr(subprocess, 'Popen', fail)
    sampler.collect('demo')
    value = sampler.snapshot()['nodes']['demo']['home']
    assert value['percent'] == 42 and value['sampled_at'] == 123
    assert value['state'] == 'error'


def test_schedule_is_five_minutes_and_manual_request_is_coalesced(tmp_path, monkeypatch):
    sampler = DiskSampler(settings()['hosts'], 'a' * 64)
    now, calls = [0], []
    class ClockStop:
        def is_set(self): return now[0] > 602
        def wait(self, seconds):
            now[0] += seconds
            if now[0] == 2:
                atomic_json(tmp_path / REQUEST_FILE, {'config_hash': 'a' * 64, 'requested_at': 10})
    sampler.stop = ClockStop()
    monkeypatch.setattr('tools.cluster.disks.time.monotonic', lambda: now[0])
    monkeypatch.setattr(sampler, 'collect', lambda alias: calls.append(now[0]))
    sampler.run(tmp_path)
    assert calls == [0, 2, 302, 602]
    assert sampler.snapshot()['completed_request'] == 10
    assert sampler.snapshot()['refreshing'] is False


def test_slow_disk_read_does_not_lock_resource_snapshot(tmp_path, monkeypatch):
    monitor = Monitor(settings())
    entered, release = threading.Event(), threading.Event()
    def slow(alias):
        entered.set()
        release.wait(3)
    monkeypatch.setattr(monitor.disks, 'collect', slow)
    thread = threading.Thread(target=monitor.disks.run, args=(tmp_path,))
    thread.start()
    try:
        assert entered.wait(1)
        snapshot = monitor.snapshot()
        assert snapshot['disk_usage']['refreshing'] is True
        assert snapshot['nodes'][0]['alias'] == 'demo'
    finally:
        monitor.disks.close()
        release.set()
        thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('parent_exits', [False, True])
@pytest.mark.parametrize('stop_early', [False, True])
def test_disk_process_tree_cleanup(tmp_path, monkeypatch, parent_exits, stop_early):
    import os
    import signal
    import time

    sampler = DiskSampler(settings()['hosts'], 'a' * 64)
    original_popen = subprocess.Popen
    marker = tmp_path / 'child'
    children = []
    def spawn(*args, **kwargs):
        source = (
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"Path({str(marker)!r}).write_text(str(child.pid)); "
            + ("" if parent_exits else "time.sleep(30)")
        )
        process = original_popen([sys.executable, '-c', source], **kwargs)
        children.append(process)
        communicate = process.communicate
        process.communicate = lambda timeout: communicate(timeout=5 if stop_early else .3)
        return process
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    thread = threading.Thread(target=sampler.collect, args=('demo',), daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        if stop_early:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists()
            sampler.close()
        thread.join(2)
        assert not thread.is_alive()
        assert time.monotonic() - started < 2
        assert not sampler.processes
        assert children[0].poll() is not None
        assert children[0].stdout.closed and children[0].stderr.closed
        assert sampler.snapshot()['nodes']['demo']['home']['state'] == 'error'
        # A terminated orphan may briefly remain as a zombie until init reaps it.
        child_stat = Path('/proc') / marker.read_text() / 'stat'
        deadline = time.monotonic() + 1
        while child_stat.exists() and child_stat.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
            time.sleep(.01)
        assert not child_stat.exists() or child_stat.read_text().split()[2] == 'Z'
    finally:
        for process in children:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        thread.join(2)


@pytest.mark.parametrize('invalid', [None, True, '12', float('nan'), float('inf'), -float('inf')])
def test_invalid_disk_does_not_discard_other_path(monkeypatch, invalid):
    sample = dict(state='available', total_bytes=1000, used_bytes=970,
                  available_bytes=-20, percent=103, sampled_at=1)
    sampler = collect_payload(monkeypatch, {'home': {**sample, 'available_bytes': invalid}, 'data': sample})
    disks = sampler.snapshot()['nodes']['demo']
    assert disks['home']['state'] == 'error'
    assert disks['home']['sampled_at'] == 123
    assert disks['home']['percent'] == 42
    assert disks['data']['state'] == 'available'
    assert disks['data']['percent'] == 103


def collect_payload(monkeypatch, payload):
    original_popen = subprocess.Popen
    def spawn(*args, **kwargs):
        return original_popen([sys.executable, '-c', 'import sys; sys.stdout.write(sys.argv[1])',
                               json.dumps(payload)], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    sampler = DiskSampler(settings()['hosts'], 'a' * 64)
    sampler.values['demo']['home'].update(percent=42, sampled_at=123, state='available')
    sampler.collect('demo')
    return sampler


def test_negative_available_capacity_is_valid(monkeypatch):
    sample = dict(state='available', total_bytes=1000, used_bytes=970,
                  available_bytes=-20, percent=103, sampled_at=1)
    sampler = collect_payload(monkeypatch, {'home': sample, 'data': {
        **sample, 'used_bytes': 100, 'available_bytes': 900, 'percent': 10}})
    disks = sampler.snapshot()['nodes']['demo']
    assert disks['home']['state'] == disks['data']['state'] == 'available'
    assert disks['home']['available_bytes'] == -20
    assert disks['home']['percent'] == 103
    assert disks['data']['percent'] == 10


@pytest.mark.parametrize('bad_sample', [None, [], {'state': []}, {'state': 'unknown'},
    {'state': 'available', 'total_bytes': -1, 'used_bytes': 0,
     'available_bytes': 0, 'percent': 0, 'sampled_at': 1}])
def test_malformed_disk_is_isolated(monkeypatch, bad_sample):
    sample = dict(state='available', total_bytes=1000, used_bytes=100,
                  available_bytes=900, percent=10, sampled_at=1)
    sampler = collect_payload(monkeypatch, {'home': bad_sample, 'data': sample})
    disks = sampler.snapshot()['nodes']['demo']
    assert disks['home']['state'] == 'error'
    assert disks['home']['sampled_at'] == 123
    assert disks['data']['state'] == 'available'
