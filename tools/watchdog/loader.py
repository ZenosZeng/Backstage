"""Load user-owned adapters from the ignored local adapters directory."""

from __future__ import annotations

import importlib
import math
from pathlib import Path
import re

from tools.watchdog.contracts import Monitor


ADAPTERS = Path(__file__).with_name("adapters")
NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("adapter/instance name must match [a-z][a-z0-9_]*")
    return name


def available_adapters() -> list[str]:
    if not ADAPTERS.is_dir():
        return []
    return sorted(
        path.stem for path in ADAPTERS.glob("*.py")
        if path.is_file() and NAME.fullmatch(path.stem)
    )


def adapter_module(name: str):
    validate_name(name)
    if name not in available_adapters():
        raise ValueError(
            f"local adapter '{name}' is not installed; add adapters/{name}.py "
            "as described in tools/watchdog/README.md"
        )
    try:
        module = importlib.import_module(f"tools.watchdog.adapters.{name}")
    except ImportError as error:
        raise ValueError(f"local adapter '{name}' could not be imported: {type(error).__name__}") from error
    if not callable(getattr(module, "create_monitor", None)):
        raise ValueError(f"adapter '{name}' must export create_monitor")
    return module


def create_monitor(name: str, config: dict, config_path: Path, directory: Path, notifier) -> Monitor:
    monitor = adapter_module(name).create_monitor(config, config_path, directory, notifier)
    if not isinstance(getattr(monitor, "name", None), str) or not monitor.name:
        raise ValueError("monitor.name must be a nonempty string")
    interval = getattr(monitor, "alert_interval", None)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("monitor.alert_interval must be finite and positive")
    if any(not callable(getattr(monitor, method, None)) for method in ("check_alerts", "status")):
        raise ValueError("monitor must provide check_alerts() and status()")
    return monitor
