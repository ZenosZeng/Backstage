"""An independent cluster sampler; the Web server reads its local snapshot."""

from __future__ import annotations

from pathlib import Path
import sys

from tools.cluster.config import read_snapshot, settings_fingerprint, validate_config
from tools.cluster.monitor import Monitor
from tools.service import Application, main


def factory(config: dict, _config_path: Path, directory: Path) -> Application:
    settings = validate_config(config)
    monitor = Monitor(settings)
    def snapshot():
        try:
            result = read_snapshot(directory)
        except FileNotFoundError:
            return {"state": "no_data", "notice": "尚无采样快照；--once 不会启动采集。"}
        except (OSError, ValueError) as error:
            return {"state": "unavailable", "error": str(error)}
        if result["config_hash"] != settings_fingerprint(settings):
            return {"state": "unavailable", "error": "快照配置与当前配置不同；需显式启动或重启采集。"}
        return result
    return Application(
        run=lambda stop: monitor.run(stop, directory),
        snapshot=snapshot,
        shutdown=monitor.close,
    )


def entry(argv=None) -> int:
    return main("cluster-monitor", ["tools.cluster"], factory, argv)


if __name__ == "__main__":
    sys.exit(entry())
