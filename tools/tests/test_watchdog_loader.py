"""Public adapter-contract tests; no private project adapters are required."""

import importlib
import math
from types import SimpleNamespace

import pytest

from tools import service
from tools.watchdog import loader
from tools.watchdog.__main__ import factory


def monitor(**changes):
    return SimpleNamespace(name="fixture", alert_interval=60, check_alerts=lambda: [], status=lambda: "ready", **changes)


@pytest.mark.parametrize("name", ["../demo", "demo.x", "demo/run", "-demo", "Demo", "", None])
def test_names_cannot_escape_local_adapter_directory(name):
    with pytest.raises(ValueError, match="name must match"):
        loader.validate_name(name)


def test_discovery_is_local_and_does_not_import_or_require_adapters(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ADAPTERS", tmp_path / "missing")
    assert loader.available_adapters() == []
    monkeypatch.setattr(loader, "ADAPTERS", tmp_path)
    for name in ("demo.py", "other.py", "_helper.py", "__init__.py", "broken-name.py"):
        (tmp_path / name).write_text("raise RuntimeError('must not import during discovery')")
    assert loader.available_adapters() == ["demo", "other"]


def test_missing_adapter_has_actionable_error():
    with pytest.raises(ValueError, match="is not installed"):
        loader.adapter_module("not_installed_adapter_fixture")


def test_missing_factory_is_rejected(monkeypatch):
    monkeypatch.setattr(loader, "available_adapters", lambda: ["demo"])
    monkeypatch.setattr(importlib, "import_module", lambda name: SimpleNamespace())
    with pytest.raises(ValueError, match="create_monitor"):
        loader.adapter_module("demo")


@pytest.mark.parametrize("interval", [0, -1, math.nan, math.inf, True, "10"])
def test_adapter_must_provide_usable_interval(tmp_path, monkeypatch, interval):
    value = monitor()
    value.alert_interval = interval
    monkeypatch.setattr(loader, "adapter_module", lambda name: SimpleNamespace(create_monitor=lambda *args: value))
    with pytest.raises(ValueError, match="finite and positive"):
        loader.create_monitor("demo", {}, tmp_path / "config.json", tmp_path, None)


@pytest.mark.parametrize("field", ["name", "check_alerts", "status"])
def test_incomplete_monitor_is_rejected(tmp_path, monkeypatch, field):
    value = monitor()
    delattr(value, field)
    monkeypatch.setattr(loader, "adapter_module", lambda name: SimpleNamespace(create_monitor=lambda *args: value))
    with pytest.raises(ValueError, match="monitor"):
        loader.create_monitor("demo", {}, tmp_path / "config.json", tmp_path, None)


def test_config_selects_adapter_and_snapshot_never_scans_or_notifies(tmp_path, monkeypatch):
    seen = []
    value = monitor()
    value.check_alerts = lambda: pytest.fail("snapshot must not scan or advance alerts")

    def build(*args):
        seen.append(args)
        return value

    def module(name):
        assert name == "demo"
        return SimpleNamespace(create_monitor=build)

    monkeypatch.setattr(loader, "adapter_module", module)
    path = tmp_path / "config.json"
    directory = tmp_path / "runtime"
    config = {"adapter": "demo", "notify": False}
    app = factory("instance")(config, path, directory)
    assert app.snapshot() == {"monitor": "fixture", "status": "ready"}
    assert seen[0][:3] == (config, path, directory)
    assert not seen[0][3].enabled
    assert not directory.exists()


@pytest.mark.parametrize("config,error", [
    ({"poll_seconds": 0}, "poll_seconds"),
    ({"poll_seconds": math.inf}, "poll_seconds"),
    ({"poll_seconds": math.nan}, "poll_seconds"),
    ({"poll_seconds": True}, "poll_seconds"),
    ({"quiet_hours": [24]}, "quiet_hours"),
    ({"quiet_hours": [True]}, "quiet_hours"),
])
def test_common_configuration_validation_precedes_adapter_side_effects(tmp_path, config, error):
    with pytest.raises(ValueError, match=error):
        factory("demo")({"notify": False, **config}, tmp_path / "config.json", tmp_path)


def test_dynamic_service_discovery_and_command_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "available_adapters", lambda: ["demo"])
    (tmp_path / "config").mkdir()
    (tmp_path / "config/watchdog-instance.json").write_text('{}')
    (tmp_path / "config/watchdog-invalid-name.json").write_text('{}')
    record = tmp_path / "tools/watchdog-orphaned/pid.json"
    record.parent.mkdir(parents=True)
    record.write_text('{}')
    assert service.service_commands("watchdog-demo") == ("tools.watchdog", "demo")
    assert service.service_commands("watchdog-../evil") is None
    names = service.service_names(tmp_path)
    assert "watchdog-demo" in names and "watchdog-instance" in names and "watchdog-orphaned" in names
    assert "watchdog-invalid-name" not in names
