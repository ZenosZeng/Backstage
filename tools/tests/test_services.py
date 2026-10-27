from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import importlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import service
from tools.config import ROOT, atomic_json, load_env_file
from tools.watchdog import notifier
from tools.watchdog.loader import ADAPTERS


DEMO = "contract_test_" + uuid.uuid4().hex
OTHER = DEMO + "_other"


@pytest.fixture(autouse=True, scope="module")
def local_test_adapters():
    source = Path(__file__).with_name("fixtures") / "monitor.py"
    ADAPTERS.mkdir(parents=True, exist_ok=True)
    paths = [ADAPTERS / f"{name}.py" for name in (DEMO, OTHER)]
    for path in paths:
        path.write_text(source.read_text())
    importlib.invalidate_caches()
    yield
    for path in paths:
        path.unlink(missing_ok=True)


def test_atomic_private_state(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"value": 1})
    atomic_json(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 2}
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(list(tmp_path.iterdir())) == 1


def test_credentials_never_evaluate_shell(tmp_path, monkeypatch):
    secret = tmp_path / "env"
    secret.write_text('export EXAMPLE_VALUE="$(touch sentinel)"\n')
    secret.chmod(0o600)
    monkeypatch.delenv("EXAMPLE_VALUE", raising=False)
    load_env_file(secret)
    assert os.environ["EXAMPLE_VALUE"] == "$(touch sentinel)"
    assert not (tmp_path / "sentinel").exists()
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_env_file(secret)


def test_pid_reuse_and_unrelated_process_are_rejected(tmp_path):
    atomic_json(
        tmp_path / "pid.json",
        {
            "name": f"watchdog-{DEMO}",
            "pid": os.getpid(),
            "start_ticks": service.process_start(os.getpid()),
            "root": str(ROOT),
        },
    )
    assert service.running_record(tmp_path, f"watchdog-{DEMO}") is None
    with patch.object(service.os, "kill") as kill:
        assert service.stop(f"watchdog-{DEMO}", tmp_path) == 0
        kill.assert_not_called()


@pytest.mark.parametrize("command,expected", [
    (b"python\0-m\0tools.cluster\0--foreground\0", True),
    (b"python\0-m\0tools.watchdog\0cluster-monitor\0--foreground\0", False),
    (b"python\0-m\0tools.cluster\0--once\0", False),
    (b"python\0app.py\0--foreground\0", False),
])
def test_cluster_service_requires_its_own_module_and_foreground_identity(tmp_path, monkeypatch, command, expected):
    pid = os.getpid()
    atomic_json(tmp_path / "pid.json", {
        "name": "cluster-monitor", "pid": pid,
        "start_ticks": service.process_start(pid), "root": str(ROOT),
    })
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(service.Path, "read_bytes", lambda self: command)
    assert bool(service.running_record(tmp_path, "cluster-monitor")) is expected


def test_sandbox_unreadable_cwd_keeps_a_live_process_visible(tmp_path, monkeypatch):
    """回归：沙箱拒绝读 /proc/<pid>/cwd 时，不能把活着的服务判成已停止。

    DSH 的 Landlock 沙箱下跑 scripts/restart-claude-feishu.sh 实测复现：cwd 读被拒
    → running_record 返回 None → status 误报未运行、stop 静默空转（打印 stopped 却不发
    SIGTERM）→ 随后的 start 撞上仍在运行的 run.lock，重启失败。其余四道指纹已足以
    确认身份，故 cwd 读不到时应放行。
    """
    atomic_json(
        tmp_path / "pid.json",
        {
            "name": "claude-feishu",
            "pid": os.getpid(),
            "start_ticks": service.process_start(os.getpid()),
            "root": str(ROOT),
        },
    )
    real_path = service.Path

    class _SandboxedProcPath:
        """只让 /proc 下的 cwd 解析失败，其余路径行为不变。"""

        def __init__(self, path):
            self._real = real_path(path)

        def read_bytes(self):
            return b"-m\0tools.claude_feishu\0--foreground\0"

        def resolve(self):
            raise PermissionError(13, "Permission denied")

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(service, "Path", _SandboxedProcPath)
    assert service.running_record(tmp_path, "claude-feishu") is not None


def test_independent_services_start_reuse_stop(tmp_path):
    config = tmp_path / "monitor.json"
    config.write_text(
        json.dumps(
            {
                "notify": False,
                "status_text": "observed",
                "poll_seconds": 0.05,
            }
        )
    )
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(tmp_path / "local")}

    def call(kind, *args):
        return subprocess.run(
            [sys.executable, "-m", "tools.watchdog", kind, *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=40,
        )

    try:
        for kind in (DEMO, OTHER):
            started = call(kind, "--config", str(config))
            assert started.returncode == 0, started.stderr + started.stdout
        record = json.loads(
            (tmp_path / f"local/tools/watchdog-{DEMO}/pid.json").read_text()
        )
        assert record["code_at_start"]["version"] == service.CODE_AT_START["version"]
        assert record["code_at_start"]["commit"] == service.CODE_AT_START["commit"]
        again = call(DEMO, "--config", str(config))
        assert again.returncode == 0 and "already running" in again.stdout
        assert str(record["pid"]) in again.stdout
        config.write_text(
            json.dumps({"notify": False, "poll_seconds": 2})
        )
        mismatch = call(DEMO, "--config", str(config))
        assert mismatch.returncode != 0 and "different configuration" in mismatch.stderr
        assert call(DEMO, "--stop").returncode == 0
        state = call(OTHER, "--status", "--json")
        assert json.loads(state.stdout)["running"] is True
        assert call(OTHER, "--stop").returncode == 0
    finally:
        call(DEMO, "--stop")
        call(OTHER, "--stop")


def make_notifier(monkeypatch, tmp_path):
    monkeypatch.setattr(
        notifier,
        "legacy_value",
        lambda key: "test-key" if key == "FEISHU_WEBHOOK_KEY" else None,
    )
    return notifier.Notifier({}, tmp_path, "TEST")


def test_feishu_checks_body_and_dedups_only_success(monkeypatch, tmp_path):
    client = make_notifier(monkeypatch, tmp_path)
    response = SimpleNamespace(read=lambda: b'{"code": 19021}')
    with patch.object(notifier.urllib.request, "urlopen") as send:
        send.return_value.__enter__.return_value = response
        assert not client.send_card("failed", "test", "test")
        response.read = lambda: b'{"code": 0}'
        assert client.send_card("failed", "test", "test")
        assert client.send_card("failed", "test", "test")
        assert send.call_count == 2


def test_notifier_exception_never_prints_secret(monkeypatch, tmp_path, capsys):
    client = make_notifier(monkeypatch, tmp_path)
    monkeypatch.setattr(notifier.time, "sleep", lambda _: None)
    with patch.object(
        notifier.urllib.request, "urlopen", side_effect=OSError("SECRET_IN_URL")
    ):
        assert not client.send_card("failed", "test", "test")
    assert "SECRET_IN_URL" not in capsys.readouterr().out


def test_remote_shutdown_kills_only_owned_child(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setenv("CLAUDE_FEISHU_STATE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("CLAUDE_FEISHU_SESSIONS", str(tmp_path / "sessions.json"))
    spec = importlib.util.spec_from_file_location(
        "isolated_bridge", ROOT / "tools/claude_feishu/bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        bridge.agent_runner.active_process = child
        bridge.shutdown()
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert bridge.agent_runner.stopping.is_set()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_restart_rejects_legacy_service_before_signalling(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "running_record", lambda *args: {"pid": 123})
    with patch.object(service.os, "kill") as kill:
        with pytest.raises(ValueError, match="too old"):
            service.restart("claude-feishu", tmp_path)
        kill.assert_not_called()


def test_reexec_restores_caller_overrides_and_releases_run_lock(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setenv("AGENTS_LOCAL_ROOT", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CLI", raising=False)
    monkeypatch.delenv("CLAUDE_FEISHU_WORKDIR", raising=False)
    monkeypatch.delenv("FEISHU_ALLOW_OPEN_IDS", raising=False)
    monkeypatch.delenv("FEISHU_OPEN_MODE", raising=False)
    monkeypatch.setenv("DSH_CLI", "caller-dsh")
    handlers = {}
    monkeypatch.setattr(service.signal, "signal", lambda signum, handler: handlers.update({signum: handler}))
    def validate(*args, **kwargs):
        assert "FEISHU_ALLOW_OPEN_IDS" not in kwargs["env"]
        assert "FEISHU_OPEN_MODE" not in kwargs["env"]
        assert kwargs["env"]["DSH_CLI"] == "caller-dsh"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(service.subprocess, "run", validate)

    def factory(*args):
        os.environ["CLAUDE_CLI"] = "injected-by-factory"
        os.environ["CLAUDE_FEISHU_WORKDIR"] = str(tmp_path)
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "old-file-user"
        os.environ["FEISHU_OPEN_MODE"] = "1"
        os.environ["DSH_CLI"] = "injected-dsh"

        def run(stop):
            handlers[signal.SIGUSR1](signal.SIGUSR1, None)
            assert stop.is_set()
            return 0

        return service.Application(run=run, snapshot=lambda: {})

    def execute(binary, argv, env):
        assert "CLAUDE_CLI" not in env
        assert "CLAUDE_FEISHU_WORKDIR" not in env
        assert "FEISHU_ALLOW_OPEN_IDS" not in env
        assert "FEISHU_OPEN_MODE" not in env
        assert env["DSH_CLI"] == "caller-dsh"
        assert argv[-2:] == ["--config", str(config)]
        with service.lock(tmp_path / "tools/claude-feishu/run.lock"):
            pass
        raise RuntimeError("re-exec boundary reached")

    monkeypatch.setattr(service.os, "execve", execute)
    with pytest.raises(RuntimeError, match="re-exec boundary reached"):
        service.main("claude-feishu", ["tools.claude_feishu"], factory,
                     ["--foreground", "--config", str(config)])


def test_restart_preflight_rejects_deleted_credential_allowlist(monkeypatch, tmp_path):
    credentials = tmp_path / "env"
    base = "FEISHU_APP_ID=test\nFEISHU_APP_SECRET=test\n"
    credentials.write_text(base + "FEISHU_ALLOW_OPEN_IDS=old-user\n")
    credentials.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "credentials_file": str(credentials), "workdir": str(tmp_path),
        "claude_cli": sys.executable,
    }))
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_ALLOW_OPEN_IDS", "FEISHU_OPEN_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENTS_LOCAL_ROOT", str(tmp_path))
    handlers = {}
    monkeypatch.setattr(service.signal, "signal", lambda signum, fn: handlers.update({signum: fn}))

    def factory(*args):
        load_env_file(credentials)

        def run(stop):
            credentials.write_text(base)
            handlers[signal.SIGUSR1](signal.SIGUSR1, None)
            assert not stop.is_set()  # failed validation keeps the old app alive
            record = json.loads((tmp_path / "tools/claude-feishu/pid.json").read_text())
            assert "validation failed" in record["restart_error"]
            stop.set()
            return 0

        return service.Application(run=run, snapshot=lambda: {})

    assert service.main("claude-feishu", ["tools.claude_feishu"], factory,
                        ["--foreground", "--config", str(config)]) == 0


def test_live_restart_keeps_pid_and_rejects_invalid_config(tmp_path):
    config = tmp_path / "monitor.json"
    valid = {"notify": False, "poll_seconds": 0.05}
    config.write_text(json.dumps(valid))
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(tmp_path / "local")}

    def call(*args):
        return subprocess.run([sys.executable, "-m", "tools.watchdog", DEMO, *args],
                              cwd=ROOT, env=env, text=True, capture_output=True, timeout=40)

    record_path = tmp_path / f"local/tools/watchdog-{DEMO}/pid.json"
    try:
        assert call("--config", str(config)).returncode == 0
        before = json.loads(record_path.read_text())
        completed = call("--restart", "--restart-timeout", "15")
        assert completed.returncode == 0, completed.stderr
        after = json.loads(record_path.read_text())
        assert after["pid"] == before["pid"]
        assert after["start_ticks"] == before["start_ticks"]
        assert after["generation"] != before["generation"]
        assert after["config_path"] == str(config)
        assert after["config_hash"] == before["config_hash"]
        config.write_text(json.dumps({**valid, "poll_seconds": -1}))
        rejected = call("--restart", "--restart-timeout", "15")
        assert rejected.returncode != 0 and "old service kept running" in rejected.stderr
        unchanged = json.loads(record_path.read_text())
        assert unchanged["generation"] == after["generation"]
        assert unchanged["ready"] is True
        assert json.loads(call("--status", "--json").stdout)["running"] is True
        config.write_text(json.dumps(valid))
        assert call("--restart", "--restart-timeout", "15").returncode == 0
    finally:
        call("--stop")


def test_landlocked_caller_requests_host_restart_without_inheriting_sandbox(tmp_path):
    """Real kernel restriction: the caller cannot write host storage before OR after restart."""
    import ctypes
    import platform

    if platform.machine() != "x86_64":
        pytest.skip("Landlock syscall numbers in this regression target Linux x86_64")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(444, 0, 0, 1) < 1:
        pytest.skip("kernel does not expose Landlock")
    local = tmp_path / "local"
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps({"notify": False, "poll_seconds": 0.05}))
    protected = tmp_path / "host-storage"
    protected.mkdir()
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(local)}
    command = [sys.executable, "-m", "tools.watchdog", DEMO]

    def call(*args):
        return subprocess.run([*command, *args], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=40)

    script = '''
import ctypes,errno,os,subprocess,sys
from pathlib import Path
libc=ctypes.CDLL(None,use_errno=True)
abi=libc.syscall(444,0,0,1)
rights=(1<<1)|sum(1<<i for i in range(4,13))
if abi>=2:rights|=1<<13
if abi>=3:rights|=1<<14
class Ruleset(ctypes.Structure):_fields_=[('handled_access_fs',ctypes.c_uint64)]
class PathRule(ctypes.Structure):
 _pack_=1
 _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]
attr=Ruleset(rights)
fd=libc.syscall(444,ctypes.byref(attr),ctypes.sizeof(attr),0)
assert fd>=0,ctypes.get_errno()
directory=os.open(sys.argv[1],os.O_PATH)
rule=PathRule(rights,directory)
assert libc.syscall(445,fd,1,ctypes.byref(rule),0)==0,ctypes.get_errno()
assert libc.prctl(38,1,0,0,0)==0
assert libc.syscall(446,fd,0)==0,ctypes.get_errno()
os.close(directory);os.close(fd)
def denied():
 try:Path(sys.argv[2]).write_text('must not write')
 except OSError as e:assert e.errno==errno.EACCES
 else:raise AssertionError('caller unexpectedly gained host write permission')
denied()
result=subprocess.run([sys.executable,'-m','tools.watchdog',sys.argv[3],'--restart','--restart-timeout','15'],
                      text=True,capture_output=True)
assert result.returncode==0,result.stderr
denied()
print('DENIED before/after; host restart ready')
'''
    try:
        assert call("--config", str(config)).returncode == 0
        record_path = local / f"tools/watchdog-{DEMO}/pid.json"
        before = json.loads(record_path.read_text())
        requested = subprocess.run([sys.executable, "-c", script, str(local), str(protected / "denied"), DEMO],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=40)
        assert requested.returncode == 0, requested.stderr
        assert "DENIED before/after" in requested.stdout
        after = json.loads(record_path.read_text())
        assert after["generation"] != before["generation"]
        assert after["pid"] == before["pid"]
        assert after["ready"] is True
        assert not (protected / "denied").exists()
    finally:
        call("--stop")
