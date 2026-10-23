"""Validate private cluster settings and read local snapshots without side effects."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re


def _number(value, name, minimum):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return value


def validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("cluster config must be an object")
    hosts = raw.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("hosts must be a non-empty list of SSH alias objects")
    selected, seen = [], set()
    for item in hosts:
        if not isinstance(item, dict):
            raise ValueError("each host must be an SSH alias object")
        alias = item.get("alias")
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", alias):
            raise ValueError("host alias must be a literal SSH configuration alias")
        if alias in seen:
            raise ValueError("host aliases must be unique")
        seen.add(alias)
        host = {"alias": alias}
        for field, default in (("label", alias), ("group", "其他")):
            value = item.get(field, default)
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"host {field} must be a non-empty string up to 200 characters")
            host[field] = value
        selected.append(host)
    settings = {
        "hosts": selected,
        "interval_seconds": _number(raw.get("interval_seconds", 5), "interval_seconds", 2),
        "health_interval_seconds": _number(raw.get("health_interval_seconds", 30), "health_interval_seconds", 10),
        "ports": None,
    }
    ports = raw.get("ports")
    if ports is not None:
        if (not isinstance(ports, dict) or not isinstance(ports.get("host"), str)
                or ports["host"] not in seen):
            raise ValueError("ports.host must name one of the configured SSH aliases")
        start, end = ports.get("start"), ports.get("end")
        if (type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= 65535 or end-start >= 256):
            raise ValueError("ports must define start/end within 1..65535 and at most 256 ports")
        public = ports.get("public_host")
        if public is not None:
            if not isinstance(public, str) or not public:
                raise ValueError("ports.public_host must be a hostname or IP, without a scheme or port")
            try:
                ipaddress.ip_address(public)
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", public):
                    raise ValueError("ports.public_host must be a hostname or IP, without a scheme or port") from None
        path = ports.get("health_path", "/healthz")
        if not isinstance(path, str) or not path.startswith("/") or any(c.isspace() for c in path) or "#" in path:
            raise ValueError("ports.health_path must be an absolute HTTP path without whitespace or fragments")
        bridge_log = ports.get("bridge_log")
        if bridge_log is not None and (
            not isinstance(bridge_log, str) or not bridge_log
            or any(c in bridge_log for c in "\x00\r\n")
            or (bridge_log != "auto" and not PurePosixPath(bridge_log).is_absolute())
        ):
            raise ValueError("ports.bridge_log must be auto or an absolute remote log path")
        settings["ports"] = {"host": ports["host"], "public_host": public,
                             "start": start, "end": end, "health_path": path, "bridge_log": bridge_log}
    return settings


def settings_fingerprint(raw: dict) -> str:
    settings = validate_config(raw)
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read_snapshot(directory: Path) -> dict:
    """Read an existing snapshot only; absence is distinct from corrupt data."""
    try:
        result = json.loads((Path(directory) / "snapshot.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("cluster snapshot is not valid JSON") from error
    if (not isinstance(result, dict) or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1 or not isinstance(result.get("state"), str)
            or result["state"] not in {"collecting", "stopped"}
            or not isinstance(result.get("nodes"), list) or not isinstance(result.get("summary"), dict)
            or (result.get("ports") is not None and not isinstance(result.get("ports"), dict))
            or not isinstance(result.get("config_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["config_hash"])):
        raise ValueError("cluster snapshot has an invalid schema")
    for key in ("time", "interval", "health_interval"):
        _number(result.get(key), f"snapshot.{key}", 0.001)
    if any(not isinstance(node, dict) for node in result["nodes"]):
        raise ValueError("cluster snapshot nodes must be objects")
    return result
