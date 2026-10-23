"""Opt-in cluster collection; importing this package never contacts a host."""

from tools.cluster.config import read_snapshot, settings_fingerprint, validate_config

__all__ = ["read_snapshot", "settings_fingerprint", "validate_config"]
