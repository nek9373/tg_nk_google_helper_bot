import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sync_do_firewall as sync


CLUSTER_ID = "5c0e91dc-b690-4429-8f58-e5016e85e38a"
PUBLIC_IP = "203.0.113.42"


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class FakeNetwork:
    def __init__(self, firewall_reads, *, ips=(PUBLIC_IP, PUBLIC_IP), put_error=None):
        self.ips = list(ips)
        self.firewall_reads = list(firewall_reads)
        self.put_error = put_error
        self.requests = []
        self.put_bodies = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(request, str):
            if request not in sync.PUBLIC_IPV4_URLS:
                raise AssertionError(f"unexpected URL: {request}")
            return FakeResponse((self.ips.pop(0) + "\n").encode("ascii"))

        method = request.get_method()
        self.assert_safe_request(request)
        if method == "GET":
            if not self.firewall_reads:
                raise AssertionError("unexpected firewall GET")
            payload = {"rules": self.firewall_reads.pop(0)}
            return FakeResponse(json.dumps(payload).encode("utf-8"))
        if method == "PUT":
            if self.put_error is not None:
                raise self.put_error
            self.put_bodies.append(json.loads(request.data))
            return FakeResponse(b"")
        raise AssertionError(f"unexpected method: {method}")

    @staticmethod
    def assert_safe_request(request):
        if request.full_url != f"{sync.API_ROOT}/databases/{CLUSTER_ID}/firewall":
            raise AssertionError(f"unexpected API URL: {request.full_url}")
        if request.get_header("Authorization") != "Bearer top-secret-token":
            raise AssertionError("missing bearer token")


class FirewallSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "mysql.json"
        self.token = root / "do_token"
        self.config.write_text(json.dumps({"cluster_id": CLUSTER_ID}), encoding="utf-8")
        self.token.write_text("top-secret-token\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_replaces_only_managed_rule_and_preserves_foreign_rules(self):
        foreign_similar = {
            "type": "ip_addr",
            "value": "192.0.2.8",
            "description": "nk_google_helper mail watcher backup",
            "uuid": "server-only-field",
        }
        foreign_tag = {"type": "tag", "value": "backend", "uuid": "another-server-field"}
        old_managed = {
            "type": "ip_addr",
            "value": "198.51.100.9",
            "description": sync.MANAGED_DESCRIPTION,
        }
        expected = [
            foreign_similar,
            {
                "type": "ip_addr",
                "value": PUBLIC_IP,
                "description": sync.MANAGED_DESCRIPTION,
            },
            foreign_tag,
        ]
        network = FakeNetwork(
            [[foreign_similar, old_managed, foreign_tag],
             [foreign_similar, old_managed, foreign_tag],
             list(reversed(expected))]
        )

        changed = sync.synchronize(self.config, self.token, opener=network)

        self.assertTrue(changed)
        self.assertEqual(len(network.put_bodies), 1)
        self.assertEqual(
            network.put_bodies[0],
            {"rules": [
                {"type": "ip_addr", "value": "192.0.2.8", "description": "nk_google_helper mail watcher backup"},
                {"type": "ip_addr", "value": PUBLIC_IP, "description": sync.MANAGED_DESCRIPTION},
                {"type": "tag", "value": "backend"},
            ]},
        )

    def test_no_put_when_rule_is_current_but_still_does_exact_readback(self):
        current = [
            {"type": "ip_addr", "value": PUBLIC_IP, "description": sync.MANAGED_DESCRIPTION},
            {"type": "tag", "value": "worker"},
        ]
        network = FakeNetwork([current, list(reversed(current))])

        changed = sync.synchronize(self.config, self.token, opener=network)

        self.assertFalse(changed)
        self.assertEqual(network.put_bodies, [])
        api_gets = [r for r, _ in network.requests if isinstance(r, urllib.request.Request)]
        self.assertEqual([r.get_method() for r in api_gets], ["GET", "GET"])

    def test_public_ip_sources_must_agree_before_any_api_request(self):
        network = FakeNetwork([], ips=("203.0.113.42", "203.0.113.43"))

        with self.assertRaisesRegex(RuntimeError, "не сошлись"):
            sync.synchronize(self.config, self.token, opener=network)

        self.assertFalse(any(isinstance(r, urllib.request.Request) for r, _ in network.requests))

    def test_ipv6_is_rejected_before_any_api_request(self):
        network = FakeNetwork([], ips=("2001:db8::1", "2001:db8::1"))

        with self.assertRaisesRegex(RuntimeError, "не IPv4"):
            sync.synchronize(self.config, self.token, opener=network)

        self.assertFalse(any(isinstance(r, urllib.request.Request) for r, _ in network.requests))

    def test_concurrent_foreign_change_aborts_before_put(self):
        old = [
            {"type": "ip_addr", "value": "198.51.100.9", "description": sync.MANAGED_DESCRIPTION},
            {"type": "tag", "value": "one"},
        ]
        changed = [old[0], {"type": "tag", "value": "two"}]
        network = FakeNetwork([old, changed])

        with self.assertRaisesRegex(RuntimeError, "между чтениями"):
            sync.synchronize(self.config, self.token, opener=network)

        self.assertEqual(network.put_bodies, [])

    def test_exact_readback_mismatch_fails(self):
        old = [{"type": "ip_addr", "value": "198.51.100.9", "description": sync.MANAGED_DESCRIPTION}]
        network = FakeNetwork([old, old, old])

        with self.assertRaisesRegex(RuntimeError, "exact readback"):
            sync.synchronize(self.config, self.token, opener=network)

        self.assertEqual(len(network.put_bodies), 1)

    def test_foreign_rule_for_current_ip_is_not_repurposed(self):
        foreign = {"type": "ip_addr", "value": PUBLIC_IP, "description": "vpn endpoint"}
        network = FakeNetwork([[foreign]])

        with self.assertRaisesRegex(RuntimeError, "занят чужим"):
            sync.synchronize(self.config, self.token, opener=network)

        self.assertEqual(network.put_bodies, [])

    def test_http_error_does_not_echo_token_or_response_body(self):
        error = urllib.error.HTTPError(
            f"{sync.API_ROOT}/databases/{CLUSTER_ID}/firewall",
            401,
            "top-secret-token in upstream reason",
            {},
            None,
        )
        network = FakeNetwork([], put_error=error)
        # The failure is needed on GET, so use a minimal opener for that case.
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            if isinstance(request, str):
                return FakeResponse((PUBLIC_IP + "\n").encode("ascii"))
            raise error

        with self.assertRaises(RuntimeError) as caught:
            sync.synchronize(self.config, self.token, opener=opener)
        message = str(caught.exception)
        self.assertNotIn("top-secret-token", message)
        self.assertNotIn("upstream reason", message)
        self.assertEqual(calls, 3)


if __name__ == "__main__":
    unittest.main()
