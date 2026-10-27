"""The small contract shared by the scheduler and local monitor adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Alert:
    key: str
    severity: str
    card_type: str
    title: str
    message: str
    lifecycle: bool = False


class Monitor(Protocol):
    name: str
    alert_interval: float

    def check_alerts(self) -> list[Alert]: ...

    def status(self) -> str: ...
