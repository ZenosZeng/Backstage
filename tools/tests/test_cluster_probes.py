"""Regression coverage for configured probes and traffic sampling intervals."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch

import pytest

from tools.cluster.monitor import Monitor


@contextmanager
def endpoint(replies):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.wfile.write(replies[min(len(requests) - 1, len(replies) - 1)])
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def configured_monitor(port, interval=5):
    return Monitor({
        "hosts": [{"alias": "fixture-node"}], "interval_seconds": interval,
        "ports": {"host": "fixture-node", "public_host": "127.0.0.1", "start": port, "end": port},
    })


OK_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"


def test_protocol_error_does_not_end_health_loop_or_prevent_recovery(monkeypatch):
    with endpoint([b"NOT HTTP\r\n\r\n", OK_RESPONSE]) as (port, requests):
        monitor = configured_monitor(port)
        failed = []

        def next_round(_timeout):
            if len(requests) == 1:
                failed.append(monitor.port_health[port].copy())
            else:
                monitor.stop.set()
            return monitor.stop.is_set()

        monkeypatch.setattr(monitor.stop, "wait", next_round)
        monitor.health_loop()

    assert requests == ["/healthz", "/healthz"]
    assert failed[0]["status"] == "down"
    assert failed[0]["checked_at"] > 0
    assert "NOT HTTP" in failed[0]["error"]
    assert monitor.port_health[port]["status"] == "up"
    assert monitor.port_health[port]["http_status"] == 200


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_health_redirect_never_contacts_unconfigured_endpoint(status):
    with endpoint([OK_RESPONSE]) as (target_port, target_requests):
        reply = (f"HTTP/1.1 {status} Redirect\r\n"
                 f"Location: http://127.0.0.1:{target_port}/outside\r\n"
                 "Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
        with endpoint([reply]) as (port, requests):
            monitor = configured_monitor(port)
            monitor.check_port(port)

    assert requests == ["/healthz"]
    assert target_requests == []
    result = monitor.port_health[port]
    assert result["status"] == "down"
    assert result["http_status"] == status
    assert result["checked_at"] > 0


def traffic_sample(received, sent):
    return {"connections": [{
        "local_ip": "192.0.2.10", "port": 31000, "peer_ip": "203.0.113.8",
        "peer_port": 50000, "received": received, "sent": sent,
    }], "events": []}


@pytest.mark.parametrize("interval", [31, 60, 300])
def test_traffic_uses_actual_elapsed_time_for_long_configured_intervals(interval):
    traffic = configured_monitor(31000, interval).traffic
    with patch("tools.cluster.monitor.time.time", return_value=100):
        traffic.update(traffic_sample(1000, 100), 100)
    with patch("tools.cluster.monitor.time.time", return_value=100 + interval):
        traffic.update(traffic_sample(1000 + interval * 1000, 100 + interval * 100), 100 + interval)

    assert traffic.snapshot["connections"] == 1
    assert traffic.snapshot["received_bps"] == 1000
    assert traffic.snapshot["sent_bps"] == 100
    assert traffic.snapshot["sources"][0]["received_bytes"] == interval * 1000


def test_long_interval_socket_failure_keeps_last_successful_counter_baseline():
    traffic = configured_monitor(31000, 60).traffic
    with patch("tools.cluster.monitor.time.time", return_value=100):
        traffic.update(traffic_sample(1000, 100), 100)
    with patch("tools.cluster.monitor.time.time", return_value=160):
        traffic.update({"connections": [], "socket_error": "temporary", "events": []}, 160)
    with patch("tools.cluster.monitor.time.time", return_value=220):
        traffic.update(traffic_sample(121000, 12100), 220)

    assert traffic.snapshot["received_bps"] == 1000
    assert traffic.snapshot["sent_bps"] == 100


@pytest.mark.parametrize("elapsed", [0, -5])
def test_nonpositive_sample_duration_does_not_replace_traffic_snapshot(elapsed):
    traffic = configured_monitor(31000, 60).traffic
    with patch("tools.cluster.monitor.time.time", return_value=100):
        traffic.update(traffic_sample(1000, 100), 100)
        before = traffic.snapshot
        traffic.update(traffic_sample(61000, 6100), 100 + elapsed)

    assert traffic.snapshot is before
    assert traffic.previous_time == 100
