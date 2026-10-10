import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import HOST_RE, check_target, create_app  # noqa: E402


class LocalTcpServer:
    """Tiny TCP server so tests never depend on the internet."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def close(self):
        self.sock.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_client(**overrides):
    tmp = tempfile.mkdtemp()
    config = {"DB_PATH": os.path.join(tmp, "test.db"), "START_CHECKER": False,
              "SEED": False, "ALLOW_PRIVATE": True, "CHECK_TIMEOUT": 1}
    config.update(overrides)
    return create_app(config).test_client()


class DeviceApiTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalTcpServer()
        self.client = make_client()

    def tearDown(self):
        self.server.close()

    def add(self, name="Local", host="127.0.0.1", port=None):
        return self.client.post("/api/devices", json={
            "name": name, "host": host, "port": port or self.server.port})

    def test_health(self):
        self.assertEqual(self.client.get("/health").get_json(), {"status": "ok"})

    def test_add_list_history_delete(self):
        resp = self.add()
        self.assertEqual(resp.status_code, 201)
        device = resp.get_json()
        self.assertEqual(device["status"], "up")
        self.assertIsNotNone(device["latency_ms"])
        self.assertEqual(device["uptime_pct"], 100.0)

        listing = self.client.get("/api/devices").get_json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(self.client.get("/api/summary").get_json(),
                         {"total": 1, "up": 1, "down": 0, "unknown": 0})

        history = self.client.get(f"/api/devices/{device['id']}/history").get_json()
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["up"])

        self.assertEqual(self.client.delete(f"/api/devices/{device['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/devices").get_json(), [])
        self.assertEqual(self.client.delete(f"/api/devices/{device['id']}").status_code, 404)

    def test_closed_port_is_down_with_reason(self):
        # Windows can take about 2 s to report "refused" on loopback,
        # so this test uses a longer timeout than the others.
        client = make_client(CHECK_TIMEOUT=5)
        resp = client.post("/api/devices", json={
            "name": "Closed", "host": "127.0.0.1", "port": free_port()})
        device = resp.get_json()
        self.assertEqual(device["status"], "down")
        self.assertEqual(device["error"], "connection refused")
        self.assertEqual(device["uptime_pct"], 0.0)

    def test_duplicate_is_conflict(self):
        self.assertEqual(self.add().status_code, 201)
        self.assertEqual(self.add(name="Again").status_code, 409)

    def test_input_validation(self):
        bad = [
            {"name": "", "host": "127.0.0.1", "port": 80},
            {"name": "x", "host": "bad host!", "port": 80},
            {"name": "x", "host": "127.0.0.1", "port": 0},
            {"name": "x", "host": "127.0.0.1", "port": 70000},
            {"name": "x", "host": "127.0.0.1", "port": True},
            {"name": "x", "host": "127.0.0.1"},
        ]
        for payload in bad:
            self.assertEqual(self.client.post("/api/devices", json=payload).status_code, 400, payload)
        self.assertEqual(self.client.post("/api/devices", data="not json").status_code, 400)

    def test_unknown_device_history_404(self):
        self.assertEqual(self.client.get("/api/devices/999/history").status_code, 404)

    def test_security_headers(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", resp.headers["Content-Security-Policy"])

    def test_dashboard_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Network Device Monitor", resp.data)
        resp.close()


class SafetyTests(unittest.TestCase):
    def test_private_addresses_blocked_by_default(self):
        client = make_client(ALLOW_PRIVATE=False)
        resp = client.post("/api/devices", json={"name": "x", "host": "127.0.0.1", "port": 80})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not allowed", resp.get_json()["error"])

    def test_check_refuses_private_target(self):
        result = check_target("127.0.0.1", 80, allow_private=False, timeout=1)
        self.assertEqual(result["up"], 0)
        self.assertIn("not allowed", result["error"])

    def test_read_only_blocks_writes(self):
        client = make_client(READ_ONLY=True)
        resp = client.post("/api/devices", json={"name": "x", "host": "127.0.0.1", "port": 80})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(client.delete("/api/devices/1").status_code, 403)
        self.assertTrue(client.get("/api/config").get_json()["read_only"])

    def test_device_limit(self):
        server = LocalTcpServer()
        self.addCleanup(server.close)
        client = make_client(MAX_DEVICES=1)
        first = client.post("/api/devices", json={"name": "a", "host": "127.0.0.1", "port": server.port})
        self.assertEqual(first.status_code, 201)
        second = client.post("/api/devices", json={"name": "b", "host": "localhost", "port": server.port})
        self.assertEqual(second.status_code, 400)
        self.assertIn("limit", second.get_json()["error"])

    def test_manual_check_cooldown(self):
        client = make_client()
        self.assertEqual(client.post("/api/check").status_code, 200)
        self.assertEqual(client.post("/api/check").status_code, 429)

    def test_host_regex(self):
        for ok in ["example.com", "1.1.1.1", "a-b.example.org", "localhost"]:
            self.assertTrue(HOST_RE.match(ok), ok)
        for bad in ["", "-bad.com", "a b", "x/y", "http://x.com", "a..b"]:
            self.assertFalse(HOST_RE.match(bad), bad)


if __name__ == "__main__":
    unittest.main()
