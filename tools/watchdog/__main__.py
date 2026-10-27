from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from tools.service import Application, main
from tools.watchdog.loader import adapter_module, available_adapters, create_monitor, validate_name
from tools.watchdog.notifier import Notifier
from tools.watchdog.runner import run


def factory(instance: str):
    validate_name(instance)

    def build(config: dict, config_path: Path, directory: Path) -> Application:
        quiet = config.get("quiet_hours")
        if quiet is not None and (
            not isinstance(quiet, list)
            or any(type(hour) is not int or not 0 <= hour <= 23 for hour in quiet)
        ):
            raise ValueError("quiet_hours must be a list of hours from 0 to 23")
        poll = config.get("poll_seconds", 60)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or poll <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        notifier = Notifier(config, config_path.parent, instance)
        monitor = create_monitor(config.get("adapter", instance), config, config_path, directory, notifier)
        return Application(
            run=lambda stop: run(monitor, notifier, stop, poll_seconds=poll, quiet_hours=quiet),
            snapshot=lambda: {"monitor": monitor.name, "status": monitor.status()},
        )

    # Request identity belongs to the adapter; service management stays generic.
    build.environment_keys = lambda config: getattr(
        adapter_module(config.get("adapter", instance)), "ENVIRONMENT_KEYS", ()
    )
    return build


def entry() -> int:
    cli = argparse.ArgumentParser(add_help=False, description="Run a local watchdog adapter")
    cli.add_argument("kind", nargs="?", help="local monitor instance name")
    cli.add_argument("--list", action="store_true", help="list installed local adapters")
    selected, rest = cli.parse_known_args()
    if selected.list:
        if selected.kind or rest:
            cli.error("--list does not accept an instance or service options")
        print(json.dumps(available_adapters()))
        return 0
    if selected.kind is None:
        cli.print_help()
        return 0 if rest == ["--help"] or rest == ["-h"] else 2
    try:
        build = factory(selected.kind)
    except ValueError as error:
        cli.error(str(error))
    return main(f"watchdog-{selected.kind}", ["tools.watchdog", selected.kind], build, rest)


if __name__ == "__main__":
    sys.exit(entry())
