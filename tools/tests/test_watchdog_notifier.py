"""Feishu payload and scheduler behavior independent of any business adapter."""

import base64
import hashlib
import hmac
import threading
import time

import pytest

from tools.watchdog import notifier, runner


def test_card_templates_and_unknown_types():
    kinds = {"start", "finished", "failed", "warning", "stalled", "heartbeat", "watchdog_started", "watchdog_stopped"}
    assert set(notifier.CARD_TEMPLATES) == kinds
    for kind in kinds:
        card = notifier.build_card(kind, "title", "body", machine="test", label="demo")
        assert card["msg_type"] == "interactive"
        assert card["card"]["schema"] == "2.0"
        assert "[test][demo]" in card["card"]["header"]["title"]["content"]
    assert notifier.CARD_TEMPLATES["failed"][0] == "red"
    assert notifier.CARD_TEMPLATES["finished"][0] == "green"
    with pytest.raises(ValueError, match="unknown card type"):
        notifier.build_card("unknown", "title", "body")


def test_signature_matches_feishu_contract_without_mutating_card():
    payload = {"msg_type": "interactive"}
    signed = notifier.sign_payload(payload, "test-secret", timestamp=123)
    expected = base64.b64encode(hmac.new(b"123\ntest-secret", digestmod=hashlib.sha256).digest()).decode()
    assert signed["timestamp"] == "123" and signed["sign"] == expected
    assert "sign" not in payload


@pytest.mark.parametrize("quiet,expected", [([6], []), ([], ["watchdog_started", "watchdog_stopped"])])
def test_lifecycle_notifications_obey_quiet_hours(monkeypatch, quiet, expected):
    local = time.struct_time((2026, 1, 1, 6, 30, 0, 3, 1, -1))
    monkeypatch.setattr(runner.time, "localtime", lambda: local)
    sent = []
    stop = threading.Event()
    stop.set()
    monitor = type("Monitor", (), {"name": "demo"})()
    sender = type("Notifier", (), {"send_card": lambda self, kind, *args: sent.append(kind) or True})()
    assert runner.run(monitor, sender, stop, quiet_hours=quiet) == 0
    assert sent == expected
